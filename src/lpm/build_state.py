"""Persistent buildpkg checkpoints, validated before any workspace cleanup."""
from __future__ import annotations

import dataclasses
import fcntl
import functools
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


class BuildStateError(RuntimeError):
    pass


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def write_state(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        Path(name).unlink(missing_ok=True)


def recipe_identity(script, metadata, stage, overrides):
    scalars, arrays, maps = metadata
    local_inputs = {}
    for raw in [*arrays.get('SOURCE', []), *arrays.get('PATCHES', [])]:
        source = raw.split('::', 1)[-1]
        if '://' not in source:
            path = Path(source)
            if not path.is_absolute():
                path = script.parent/path
            if path.is_file():
                local_inputs[raw] = file_hash(path)
    return dict(recipe_hash=file_hash(script), local_inputs=local_inputs,
                name=scalars.get('NAME'), version=scalars.get('VERSION'),
                release=scalars.get('RELEASE', '1'), arch=scalars.get('ARCH', ''),
                stage=stage, overrides=dataclasses.asdict(overrides) if overrides else None,
                source=arrays.get('SOURCE', []), patches=arrays.get('PATCHES', []),
                options=arrays.get('BUILD_OPTIONS', []))


class BuildSession:
    def __init__(self, state_dir, identity, *, resume=False, restart=False, workspace=None):
        name = identity['name']
        if not name or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]*', name):
            raise BuildStateError('Invalid package name for build state')
        self.directory = Path(state_dir)/'builds'/name
        self.path = self.directory/'state.json'
        self.identity = identity
        self.resume = resume
        self.restart = restart
        self.data = {}
        self.lock = None
        self.closed = False
        root = Path(workspace).expanduser().resolve()/name if workspace else None
        self.expected = {label: (root/label if root else Path('/tmp')/f'{label}-{name}').resolve()
                         for label in ('pkg', 'build', 'src')}

    def __enter__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = (self.directory/'lock').open('a')
        os.fchmod(self.lock.fileno(), 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise BuildStateError('A build of this package is already running') from None
        try:
            try:
                old = json.loads(self.path.read_text()) if self.path.exists() else None
                if old is not None and not isinstance(old, dict):
                    raise ValueError('Build state must be an object')
            except (ValueError, OSError) as exc:
                if not self.restart:
                    raise BuildStateError(f'Unreadable build state; no workspace cleanup performed: {exc}') from exc
                old = None
            if self.resume:
                if not old:
                    raise BuildStateError('No tracked build to resume; no files were removed')
                if old.get('schema') != 1 or old.get('identity') != self.identity:
                    raise BuildStateError('Recipe, local sources, package identity or CPU overrides changed; resume refused before cleanup')
                if not isinstance(old.get('completed'), list) or not isinstance(old.get('directories'), dict) or not isinstance(old.get('token'), str):
                    raise BuildStateError('Invalid build checkpoint; no workspace cleanup performed')
                self.data = old
                self.validate_directories()
                if self.data.get('directories') and 'source' not in self.data.get('completed', []):
                    raise BuildStateError('Source preparation was interrupted; use --restart explicitly to re-extract')
                self.validate_sources()
            else:
                if old and old.get('status') not in ('packaged', 'complete') and old.get('directories') and not self.restart:
                    raise BuildStateError('An unfinished build exists. Use --resume to keep it or --restart to discard it')
                # Protect kernel objects produced outside buildpkg's checkpoint system.
                src = self.expected['src']
                if not old and not self.restart and any(src.glob('*/_build/.kmake.json')):
                    raise BuildStateError('Untracked kmake build exists; refusing automatic cleanup. Preserve it or explicitly use --restart')
                self.data = dict(schema=1, token=uuid.uuid4().hex, identity=self.identity,
                                 completed=[], status='running', active='dependencies',
                                 directories={}, created=time.time())
            self.data['status'] = 'running'
            self.data['attempts'] = self.data.get('attempts', 0) + 1
            self.data.pop('error', None)
            self.save()
            return self
        except BaseException:
            self.release()
            raise

    def __exit__(self, kind, error, traceback):
        if error is not None and not self.closed:
            self.data['status'] = 'interrupted' if isinstance(error, KeyboardInterrupt) else 'failed'
            self.data['error'] = str(error)[:2000]
            active = self.data.get('active')
            if active:
                self.data.setdefault('phases', {}).setdefault(active, {}).update(status=self.data['status'], error=self.data['error'], finished=time.time())
            self.save()
        self.release()

    def release(self):
        if self.lock is not None:
            self.lock.close()
            self.lock = None

    def save(self):
        self.data['updated'] = time.time()
        write_state(self.path, self.data)

    def done(self, phase):
        return phase in self.data.get('completed', [])

    def begin(self, phase):
        self.data.update(active=phase, status='running')
        record = self.data.setdefault('phases', {}).setdefault(phase, {})
        record.update(status='running', started=time.time(), attempts=record.get('attempts', 0)+1)
        record.pop('error', None)
        self.save()

    def complete(self, phase):
        if not self.done(phase):
            self.data['completed'].append(phase)
        self.data.setdefault('phases', {}).setdefault(phase, {}).update(status='complete', finished=time.time())
        self.data['active'] = None
        self.save()

    def bind(self, paths):
        self.data['directories'] = {}
        for label, path in paths.items():
            path = Path(path).resolve()
            self.validate_path(label, path)
            st = path.stat()
            self.data['directories'][label] = dict(path=str(path), device=st.st_dev, inode=st.st_ino)
        self.save()

    def refresh(self, label, path):
        st = Path(path).stat()
        self.data['directories'][label] = dict(path=str(Path(path).resolve()), device=st.st_dev, inode=st.st_ino)
        self.save()

    def validate_path(self, label, path):
        if label not in self.expected or path == Path('/'):
            raise BuildStateError('Invalid saved workspace path')
        fallback = (path.parent == Path(tempfile.gettempdir()).resolve()
                    and path.name.startswith(f'lpm-{label}-{self.identity["name"]}-'))
        if path != self.expected[label] and not fallback:
            raise BuildStateError(f'Saved {label} path is outside the configured workspace: {path}')

    def validate_directories(self):
        directories = self.data.get('directories', {})
        if directories and set(directories) != {'pkg', 'build', 'src'}:
            raise BuildStateError('Incomplete workspace checkpoint; no cleanup performed')
        for label, record in self.data.get('directories', {}).items():
            path = Path(record['path'])
            self.validate_path(label, path)
            disposable_stage = label == 'pkg' and not self.done('package')
            if path.resolve() != path or path.is_symlink():
                raise BuildStateError(f'Saved {label} directory is redirected: {path}')
            if not path.is_dir():
                if disposable_stage and not path.exists():
                    continue  # Recreated only at the validated staging retry boundary.
                raise BuildStateError(f'Saved {label} directory is missing: {path}')
            st = path.stat()
            if (st.st_dev, st.st_ino) != (record['device'], record['inode']) and not disposable_stage:
                raise BuildStateError(f'Saved {label} directory was replaced; resume refused: {path}')

    def record_sources(self, srcroot):
        self.data['source_files'] = {str(p): file_hash(p) for p in Path(srcroot).iterdir()
                                     if p.is_file() and not p.is_symlink()}
        self.complete('source')

    def validate_sources(self):
        for filename, expected in self.data.get('source_files', {}).items():
            path = Path(filename)
            if not path.is_file() or file_hash(path) != expected:
                raise BuildStateError(f'Staged source input changed or disappeared: {path}')
        for filename in self.data.get('kmake_states', []):
            if not Path(filename).is_file():
                raise BuildStateError(f'Kernel output state disappeared: {filename}')

    def record_kmake(self, srcroot):
        self.data['kmake_states'] = [str(p) for p in Path(srcroot).glob('*/_build/.kmake.json')]
        self.save()

    def environment(self, env):
        keys = ('ARCH', 'CFLAGS', 'CXXFLAGS', 'CPPFLAGS', 'LDFLAGS', 'CC', 'CXX',
                'HOSTCC', 'HOSTCXX', 'CROSS_COMPILE', 'LLVM', 'LLVM_IAS',
                'LPM_BUILD_STAGE', 'LPM_CPU_MARCH', 'LPM_CPU_MTUNE')
        identity = {key: env.get(key) for key in keys}
        # Fingerprint the effective compiler even for non-kmake recipes.
        compiler = shlex.split(env.get('CC', 'cc'))
        if compiler:
            executable = shutil.which(compiler[0], path=env.get('PATH'))
            if not executable and compiler[0].startswith('/build/'):
                executable = str(Path(self.data['directories']['build']['path'])/compiler[0][7:])
            if executable and Path(executable).is_file():
                identity['compiler_file'] = file_hash(executable)
                try:
                    result = subprocess.run([executable, *compiler[1:], '--version'],
                                            env=env, text=True, capture_output=True, timeout=20, check=False)
                    if result.returncode == 0 and isinstance(result.stdout, (str, bytes)):
                        identity['compiler_version'] = result.stdout.decode() if isinstance(result.stdout, bytes) else result.stdout
                except (OSError, subprocess.TimeoutExpired):
                    pass
        old = self.data.get('environment')
        if self.resume and old is not None and old != identity:
            raise BuildStateError('Build flags or compiler changed; resume refused. Job-count changes are allowed')
        self.data['environment'] = identity
        self.save()

    def package_available(self):
        if not self.resume or not self.done('package'):
            return False
        for record in [self.data['package'], *self.data.get('splits', [])]:
            if not Path(record['path']).is_file() or file_hash(record['path']) != record['sha256']:
                raise BuildStateError(f'Completed package changed or disappeared: {record["path"]}')
        return True

    def finish(self, out, meta, splits):
        def record(path, metadata):
            return dict(path=str(Path(path).resolve()), sha256=file_hash(path), meta=dataclasses.asdict(metadata))
        self.data['package'] = record(out, meta)
        self.data['splits'] = [record(path, metadata) for path, metadata in splits]
        self.complete('package')
        self.data['status'] = 'packaged'
        self.save()
        # Release before install callbacks or automatic stage 2 builds of this name.
        self.closed = True
        self.release()


def resumable_build(function):
    @functools.wraps(function)
    def run(*args, **kwargs):
        app = sys.modules[function.__module__]
        bound = inspect.signature(function).bind(*args, **kwargs)
        bound.apply_defaults()
        options = bound.arguments
        script = Path(options['script']).resolve()
        had_split = 'LPM_SPLIT_PACKAGE' in os.environ
        if not had_split:
            os.environ['LPM_SPLIT_PACKAGE'] = shutil.which('true') or '/bin/true'
        try:
            reader = app._capture_lpmbuild_metadata
            parameters = inspect.signature(reader).parameters
            stage_aware = 'build_stage' in parameters or any(p.kind == p.VAR_KEYWORD for p in parameters.values())
            metadata = reader(script, build_stage=options['_build_stage']) if stage_aware else reader(script)
        finally:
            if not had_split:
                os.environ.pop('LPM_SPLIT_PACKAGE', None)
        name = metadata[0].get('NAME', '')
        if name in (options.get('_building_stack') or ()):
            app.die('dependency cycle detected: ' + ' -> '.join((*options['_building_stack'], name)))
        resume = options['resume']
        if os.environ.get('KMAKE_RESUME') == '1' and not resume and not options['is_dep']:
            raise BuildStateError('KMAKE_RESUME is recipe-only. Use buildpkg --resume to protect sources before extraction')
        if resume and options['force_rebuild']:
            raise BuildStateError('--resume cannot be combined with --force-rebuild')
        if resume and options['restart']:
            raise BuildStateError('--resume and --restart cannot be combined')
        identity = recipe_identity(script, metadata, options['_build_stage'], options['cpu_overrides'])
        session = BuildSession(app._current_state_dir(), identity, resume=resume, restart=options['restart'],
                               workspace=os.environ.get('LPM_BUILD_WORKDIR') or app.CONF.get('LPM_BUILD_WORKDIR'))
        if options['_resume_available'] and session.path.is_file() and not options['restart']:
            previous = json.loads(session.path.read_text())
            if previous.get('identity') == identity:
                session.resume = True
                options['resume'] = True
        if options['build_status']:
            return json.loads(session.path.read_text()) if session.path.exists() else {'status': 'not-started', 'state_file': str(session.path)}
        with session:
            options['_session'] = session
            options['_metadata'] = metadata
            return function(**options)
    return run
