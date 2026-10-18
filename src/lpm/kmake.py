"""Resumable Kbuild orchestration; packaging and boot deployment remain with LPM."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from .accelerators import available_memory_mb
from .parallel import cpu_count

SCHEMA = 1
IMAGES = {'x86_64': ('bzImage', 'arch/x86/boot/bzImage'),
          'arm64': ('Image', 'arch/arm64/boot/Image')}
# Kbuild, not distro package flags, owns the target compiler options.
STRIP_ENV = ('CFLAGS', 'CXXFLAGS', 'CPPFLAGS', 'LDFLAGS', 'KCFLAGS', 'KCPPFLAGS',
             'MAKEFLAGS', 'MFLAGS', 'MAKEOVERRIDES', 'KBUILD_OUTPUT',
             'KCONFIG_CONFIG', 'LOCALVERSION', 'ARCH', 'KBUILD_EXTMOD')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path, data):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.kmake-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be positive')
    return number


def resource_jobs(explicit=None, per_job=1536):
    if explicit is not None:
        jobs = int(explicit)
        if jobs < 1:
            raise ValueError('Build jobs must be positive')
        return jobs
    cpus = cpu_count()
    memory = available_memory_mb()
    return cpus if memory is None else max(1, min(cpus, int(memory * .8) // per_job))


def build_environment(cache='auto', cache_dir=None):
    env = dict(os.environ)
    if env.get('LLVM'):
        raise ValueError('LLVM toolchains are not supported in this first kmake version; use GCC')
    for key in STRIP_ENV:
        env.pop(key, None)
    env['LPM_BUILD_PROFILE'] = 'kernel'
    selected = None
    if cache != 'none':
        for candidate in (('ccache', 'sccache') if cache == 'auto' else (cache,)):
            selected = shutil.which(candidate, path=env.get('PATH'))
            if selected:
                break
        if not selected and cache != 'auto':
            raise RuntimeError(f'Requested compiler cache is unavailable: {cache}')
    if selected:
        mode = Path(selected).name
        directory = Path(cache_dir or env.get('LPM_COMPILER_CACHE_DIR') or
                         Path(env.get('XDG_CACHE_HOME', str(Path.home()/'.cache')))/'lpm/compiler'/mode).expanduser().resolve()
        if directory == Path('/'):
            raise ValueError('Compiler cache directory cannot be /')
        directory.mkdir(parents=True, exist_ok=True)
        env['CCACHE_DIR' if mode == 'ccache' else 'SCCACHE_DIR'] = str(directory)
        if mode == 'ccache':
            env.setdefault('CCACHE_COMPILERCHECK', 'content')
        for key, default in (('CC', env.get('CROSS_COMPILE', '')+'gcc'),
                             ('HOSTCC', 'gcc'), ('HOSTCXX', 'g++')):
            command = shlex.split(env.get(key, default))
            if not command:
                raise ValueError(f'Empty compiler command: {key}')
            if Path(command[0]).name not in ('ccache', 'sccache'):
                env[key] = shlex.join([selected, *command])
    return env, selected


class KernelBuild:
    def __init__(self, args):
        self.args = args
        self.source = Path(args.source).resolve()
        self.output = Path(args.output).resolve() if args.output else self.source/'_build'
        if self.output == self.source or self.output in self.source.parents:
            raise ValueError('Output must be separate from the source and its ancestors')
        if not (self.source/'Makefile').is_file() or not (self.source/'scripts/config').is_file():
            raise ValueError('Source must be a Linux kernel tree with scripts/config')
        self.path = self.output/'.kmake.json'
        self.state = {}
        self.env = None

    @contextmanager
    def locked(self):
        self.output.mkdir(parents=True, exist_ok=True)
        with (self.output/'.kmake.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.path.exists():
                self.state = json.loads(self.path.read_text())
                if self.state.get('schema') != SCHEMA:
                    raise ValueError('Unsupported kmake state schema')
                if self.state.get('source') != str(self.source):
                    raise ValueError('This output directory belongs to another source tree')
            yield

    def save(self):
        self.state['updated'] = time.time()
        atomic_json(self.path, self.state)

    def command(self, *targets, capture=False, jobs=None, variables=()):
        arch = self.args.arch or self.state.get('arch') or {'aarch64': 'arm64', 'amd64': 'x86_64'}.get(platform.machine(), platform.machine())
        if arch not in IMAGES:
            raise ValueError('Supported architectures: x86_64, arm64')
        local = self.args.localversion if self.args.localversion is not None else self.state.get('localversion', '')
        if not re.fullmatch(r'[A-Za-z0-9._+-]*', local):
            raise ValueError('Invalid local version suffix')
        command = ['make', '-C', str(self.source), f'O={self.output}',
                   f'ARCH={arch}', f'LOCALVERSION={local}']
        if jobs:
            command += [f'-j{jobs}']
        if self.args.load_average:
            command += [f'-l{self.args.load_average}']
        for key in ('CC', 'HOSTCC', 'HOSTCXX', 'CROSS_COMPILE', 'LLVM', 'LLVM_IAS'):
            if self.env.get(key):
                command.append(f'{key}={self.env[key]}')
        command += list(variables) + list(targets)
        print('[kmake] '+shlex.join(command), file=sys.stderr, flush=True)
        result = subprocess.run(command, env=self.env, check=True, text=True,
                                stdout=subprocess.PIPE if capture else None)
        return result.stdout.strip() if capture else None

    def identity(self):
        # Compiler identity detects toolchain upgrades even at an unchanged path.
        command = shlex.split(self.env.get('CC', self.env.get('CROSS_COMPILE', '')+'gcc'))
        version = subprocess.run([*command, '--version'], env=self.env, check=True,
                                 text=True, stdout=subprocess.PIPE).stdout
        return {'command': command, 'executable': shutil.which(command[0], path=self.env.get('PATH')), 'version': version}

    def configure(self):
        if self.args.config and self.args.defconfig:
            raise ValueError('Choose --config or --defconfig')
        if not self.args.config and not self.args.defconfig and not (self.output/'.config').is_file():
            raise ValueError('Supply --config FILE or explicitly choose --defconfig')
        baseline = Path(self.args.config).resolve() if self.args.config else None
        fragments = [Path(path).resolve() for path in self.args.fragment]
        for path in ([baseline] if baseline else []) + fragments:
            if not path.is_file():
                raise ValueError(f'Missing configuration input: {path}')
        if baseline == self.output/'.config':
            baseline = None  # Resume/edit the existing configuration in place.
        arch = self.args.arch or self.state.get('arch') or {'aarch64':'arm64', 'amd64':'x86_64'}.get(platform.machine(), platform.machine())
        if arch not in IMAGES:
            raise ValueError('Supported architectures: x86_64, arm64')
        self.state = dict(self.state, schema=SCHEMA, source=str(self.source), output=str(self.output),
                          arch=arch, localversion=self.args.localversion if self.args.localversion is not None else self.state.get('localversion', ''),
                          configured=False, built=False, staged=False)
        self.save()
        config = self.output/'.config'
        if config.exists():
            shutil.copy2(config, self.output/'.config.kmake-backup')
        if baseline:
            shutil.copyfile(baseline, config)
        elif self.args.defconfig:
            self.command('defconfig')
        if fragments:
            subprocess.run(['bash', str(self.source/'scripts/kconfig/merge_config.sh'),
                            '-m', '-O', str(self.output), str(config), *map(str, fragments)],
                           cwd=self.source, env=self.env, check=True)
        # Avoid embedded distro certificate paths and unstable git suffixes.
        subprocess.run(['bash', str(self.source/'scripts/config'), '--file', str(config),
                        '--disable', 'LOCALVERSION_AUTO', '--set-str', 'LOCALVERSION', '',
                        '--set-str', 'SYSTEM_TRUSTED_KEYS', '', '--set-str', 'SYSTEM_REVOCATION_KEYS', ''],
                       env=self.env, check=True)
        self.command('olddefconfig')
        self.validate_config()
        release = self.command('-s', '--no-print-directory', 'kernelrelease', capture=True)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]*', release):
            raise ValueError(f'Invalid kernel release: {release!r}')
        self.state.update(configured=True, release=release, config_hash=digest(config),
                          makefile_hash=digest(self.source/'Makefile'), compiler=self.identity(),
                          inputs={str(p):digest(p) for p in ([baseline] if baseline else [])+fragments})
        self.save()

    def validate_config(self):
        config = set((self.output/'.config').read_text().splitlines())
        required = ['MODULES', 'BLK_DEV_INITRD', 'RD_ZSTD', 'DEVTMPFS', 'PROC_FS',
                    'SYSFS', 'TMPFS', 'CGROUPS'] + list(getattr(self.args, 'require', []))
        for symbol in required:
            if not re.fullmatch(r'[A-Z0-9_]+', symbol):
                raise ValueError(f'Invalid Kconfig symbol: {symbol}')
            if f'CONFIG_{symbol}=y' not in config:
                raise ValueError(f'Required CONFIG_{symbol}=y is unavailable after olddefconfig')

    def ready(self):
        if not self.state.get('configured'):
            raise ValueError('Run kmake configure first')
        if self.args.arch and self.args.arch != self.state['arch']:
            raise ValueError('Architecture changed; run configure first')
        if self.args.localversion is not None and self.args.localversion != self.state['localversion']:
            raise ValueError('Local version changed; run configure first')
        if digest(self.output/'.config') != self.state['config_hash']:
            raise ValueError('Configuration changed; run configure (without --config to retain edits)')
        if digest(self.source/'Makefile') != self.state['makefile_hash']:
            raise ValueError('Kernel version/source Makefile changed; run configure first')
        if self.identity() != self.state['compiler']:
            raise ValueError('Compiler changed; run configure first')

    def build(self):
        self.ready()
        self.state.update(built=False, staged=False)
        self.save()
        jobs = resource_jobs(self.args.jobs or os.environ.get('LPM_BUILD_JOBS'), self.args.memory_per_job)
        print(f'[kmake] jobs={jobs}; incremental Kbuild; existing objects retained', file=sys.stderr)
        self.command(IMAGES[self.state['arch']][0], 'modules', jobs=jobs)
        self.check_artifacts()
        self.state.update(built=True, config_hash=digest(self.output/'.config'), jobs=jobs)
        self.save()

    def check_artifacts(self):
        for name in (IMAGES[self.state['arch']][1], 'System.map', 'modules.order',
                     'modules.builtin', 'include/config/kernel.release'):
            path = self.output/name
            if not path.is_file() or (not path.stat().st_size and name not in ('modules.order','modules.builtin')):
                raise ValueError(f'Missing build artifact: {path}')
        actual = (self.output/'include/config/kernel.release').read_text().strip()
        if actual != self.state['release']:
            raise ValueError(f'Kernel release mismatch: {actual}')
        with (self.output/IMAGES[self.state['arch']][1]).open('rb') as stream:
            header = stream.read(0x206)
        if self.state['arch'] == 'x86_64' and header[0x202:0x206] != b'HdrS':
            raise ValueError('Invalid x86 kernel boot header')
        if self.state['arch'] == 'arm64' and header[0x38:0x3c] != b'ARM\x64':
            raise ValueError('Invalid arm64 kernel image header')

    def stage(self):
        self.ready()
        if not self.state.get('built'):
            raise ValueError('No completed build; run kmake build first')
        self.check_artifacts()
        dest = Path(self.args.destdir).resolve()
        if dest == Path('/') or dest == self.source or dest == self.output or dest in self.source.parents or dest in self.output.parents:
            raise ValueError('DESTDIR must be a separate package staging directory, not /')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]*', self.args.pkgbase):
            raise ValueError('Invalid pkgbase')
        # Refuse directory symlinks which could redirect staging into the host.
        module_dir = dest/'usr/lib/modules'/self.state['release']
        if module_dir.resolve() != module_dir:
            raise ValueError('Staging path contains a symlink')
        if module_dir.exists() and any(module_dir.iterdir()):
            raise ValueError('Kernel staging directory is not empty; use a fresh package DESTDIR')
        self.state['staged'] = False
        self.save()
        module_dir.mkdir(parents=True, exist_ok=True)
        self.command('modules_install', jobs=1, variables=(f'INSTALL_MOD_PATH={dest}/usr', 'INSTALL_MOD_STRIP=1', 'DEPMOD=true'))
        for name in ('build', 'source'):
            path = module_dir/name
            if path.is_symlink() or path.is_file():
                path.unlink()
        for source, target in ((IMAGES[self.state['arch']][1], 'vmlinuz'), ('System.map','System.map'), ('.config','config')):
            path = module_dir/target
            if path.is_symlink():
                raise ValueError(f'Refusing staged file symlink: {path}')
            shutil.copyfile(self.output/source, path)
            path.chmod(0o644)
        if (module_dir/'pkgbase').is_symlink():
            raise ValueError('Refusing staged pkgbase symlink')
        (module_dir/'pkgbase').write_text(self.args.pkgbase+'\n')
        subprocess.run(['depmod', '-b', str(dest/'usr'), '-F', str(self.output/'System.map'), self.state['release']], env=self.env, check=True)
        self.state.update(staged=True, destdir=str(dest))
        self.save()


def parser():
    p = argparse.ArgumentParser(prog='kmake', description=__doc__)
    p.add_argument('--source', default='.', help='kernel source directory')
    p.add_argument('--output', help='separate output directory; default SOURCE/_build')
    p.add_argument('--arch', choices=tuple(IMAGES))
    p.add_argument('--localversion', help='suffix such as -lpm2; defaults to recorded suffix')
    p.add_argument('--jobs', type=positive)
    p.add_argument('--memory-per-job', type=positive, default=1536, help='automatic job budget in MiB')
    p.add_argument('--load-average', type=positive, help='GNU make load limit')
    p.add_argument('--cache', choices=('auto','none','ccache','sccache'), default=os.environ.get('LPM_COMPILER_CACHE', 'auto'))
    p.add_argument('--cache-dir')
    sub = p.add_subparsers(dest='action', required=True)
    c = sub.add_parser('configure', help='import or retain config, merge fragments, resolve Kconfig')
    c.add_argument('--config')
    c.add_argument('--fragment', action='append', default=[])
    c.add_argument('--defconfig', action='store_true', help='explicitly opt into architecture defaults')
    c.add_argument('--require', action='append', default=[], help='additional symbol required built-in')
    sub.add_parser('build', help='resume incremental kernel and module compilation')
    sub.add_parser('check', help='validate completed build artifacts')
    sub.add_parser('status', help='print recorded state and current configuration fingerprint')
    s = sub.add_parser('stage', help='stage modules/image for an LPM package; never boot deployment')
    s.add_argument('--destdir', required=True)
    s.add_argument('--pkgbase', default='linux')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        build = KernelBuild(args)
        with build.locked():
            if args.action == 'status':
                state = dict(build.state)
                if (build.output/'.config').exists():
                    state['current_config_hash'] = digest(build.output/'.config')
                    state['config_matches'] = state['current_config_hash'] == state.get('config_hash')
                print(json.dumps(state, indent=2, sort_keys=True))
                return 0
            build.env, cache = build_environment(args.cache, args.cache_dir)
            print(f'[kmake] compiler cache: {cache or "none"}', file=sys.stderr)
            if args.action == 'check':
                build.ready()
                if not build.state.get('built'):
                    raise ValueError('No completed build')
                build.check_artifacts()
            else:
                getattr(build, args.action)()
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'[kmake] ERROR: {exc}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('[kmake] Interrupted; existing objects retained. Rerun build to resume.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
