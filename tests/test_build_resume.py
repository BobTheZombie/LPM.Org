import importlib
import io
import json
import os
from pathlib import Path
import shutil
import tarfile

import pytest

from lpm.build_state import BuildSession, BuildStateError


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    app = importlib.import_module('lpm.app')
    monkeypatch.setenv('LPM_STATE_DIR', str(tmp_path/'state'))
    monkeypatch.setenv('LPM_BUILD_WORKDIR', str(tmp_path/'work'))
    monkeypatch.setenv('LPM_COMPILER_CACHE', 'none')
    monkeypatch.delenv('KMAKE_RESUME', raising=False)
    monkeypatch.setattr(app, 'ENABLE_CPU_OPTIMIZATIONS', False)
    monkeypatch.setattr(app, 'run_hook', lambda *args, **kwargs: None)
    packages = []
    def package(root, meta, out, sign=True):
        packages.append(str(out))
        out.write_bytes(b'package')
    monkeypatch.setattr(app, 'build_package', package)
    monkeypatch.setattr(app, 'generate_install_script', lambda root: '#!/bin/sh\nexit 0\n')
    archive=tmp_path/'demo-1.tar.gz'
    with tarfile.open(archive,'w:gz') as tf:
        data=b'original\n'
        item=tarfile.TarInfo('demo-1/input.txt'); item.size=len(data)
        tf.addfile(item,io.BytesIO(data))
    patch=tmp_path/'fix.patch'
    patch.write_text('--- a/input.txt\n+++ b/input.txt\n@@ -1 +1 @@\n-original\n+patched\n')
    recipe=tmp_path/'demo.lpmbuild'
    recipe.write_text('''NAME=resume-demo
VERSION=1
RELEASE=1
ARCH=noarch
SOURCE=("demo-1.tar.gz" "fix.patch")
PATCHES=("fix.patch")
REQUIRES=()
BUILD_REQUIRES=()
BUILD_OPTIONS=()
prepare() {
    set -eu
    printf 'prepare\\n' >> "$SRCROOT/phases.log"
    test "$(cat "$SRCROOT/$NAME-$VERSION/input.txt")" = patched
    mkdir -p "$SRCROOT/$NAME-$VERSION/objects"
}
build() {
    set -eu
    printf 'build\\n' >> "$SRCROOT/phases.log"
    object="$SRCROOT/$NAME-$VERSION/objects/first.o"
    [ -f "$object" ] || printf 'completed-object\\n' > "$object"
    if [ "${FAIL_BUILD:-0}" = 1 ]; then return 7; fi
    printf 'done\\n' > "$SRCROOT/$NAME-$VERSION/binary"
}
check() {
    printf 'check\\n' >> "$SRCROOT/phases.log"
    [ "${FAIL_CHECK:-0}" != 1 ]
}
staging() {
    set -eu
    printf 'staging\\n' >> "$SRCROOT/phases.log"
    test ! -e "$pkgdir/partial"
    if [ "${FAIL_STAGE:-0}" = 1 ]; then
        printf 'stale\\n' > "$pkgdir/partial"
        return 8
    fi
    mkdir -p "$pkgdir/usr/bin"
    cp "$SRCROOT/$NAME-$VERSION/binary" "$pkgdir/usr/bin/demo"
}
''')
    src=tmp_path/'work/resume-demo/src'
    state=tmp_path/'state/builds/resume-demo/state.json'
    def run(**kwargs):
        return app.run_lpmbuild(recipe,tmp_path,build_deps=False,prompt_install=False,**kwargs)
    return app,recipe,src,state,run,packages


def test_failed_build_resume_preserves_objects_and_does_not_reextract(pipeline, monkeypatch):
    app,recipe,src,state,run,packages=pipeline
    monkeypatch.setenv('FAIL_BUILD','1')
    with pytest.raises(SystemExit): run()
    object=src/'resume-demo-1/objects/first.o'; before=object.stat().st_mtime_ns
    assert json.loads(state.read_text())['active']=='build'
    # Normal retries are no longer allowed to silently discard this workspace.
    with pytest.raises(BuildStateError,match='unfinished'): run()
    monkeypatch.delenv('FAIL_BUILD')
    monkeypatch.setenv('LPM_BUILD_JOBS','6')
    monkeypatch.setattr(app,'_maybe_fetch_source',lambda *a,**kw: pytest.fail('resume fetched sources'))
    monkeypatch.setattr(app,'_apply_lpmbuild_patches',lambda *a,**kw: pytest.fail('resume reapplied patches'))
    out,*_=run(resume=True)
    assert out.is_file() and packages
    assert object.stat().st_mtime_ns==before
    assert (src/'phases.log').read_text().splitlines()==['prepare','build','build','check','staging']
    assert json.loads(state.read_text())['status']=='packaged'


def test_resume_check_failure_does_not_rebuild(pipeline,monkeypatch):
    _,_,src,_,run,_=pipeline
    monkeypatch.setenv('FAIL_CHECK','1')
    with pytest.raises(SystemExit): run()
    monkeypatch.delenv('FAIL_CHECK')
    run(resume=True)
    assert (src/'phases.log').read_text().splitlines()==['prepare','build','check','check','staging']


def test_resume_stage_failure_uses_clean_staging_only(pipeline,monkeypatch):
    _,_,src,state,run,_=pipeline
    monkeypatch.setenv('FAIL_STAGE','1')
    with pytest.raises(SystemExit): run()
    data=json.loads(state.read_text()); pkg=Path(data['directories']['pkg']['path'])
    assert (pkg/'partial').is_file()
    monkeypatch.delenv('FAIL_STAGE')
    run(resume=True)
    assert not (pkg/'partial').exists()
    assert (src/'phases.log').read_text().splitlines()==['prepare','build','check','staging','staging']


def test_resume_package_failure_restages_without_rebuild(pipeline,monkeypatch):
    app,_,src,_,run,_=pipeline
    original=app.build_package
    monkeypatch.setattr(app,'build_package',lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('package interrupted')))
    with pytest.raises(RuntimeError,match='package interrupted'): run()
    monkeypatch.setattr(app,'build_package',original)
    run(resume=True)
    assert (src/'phases.log').read_text().splitlines()==['prepare','build','check','staging','staging']


def test_completed_package_is_verified_and_reused(pipeline):
    _,_,src,_,run,packages=pipeline
    run()
    phases=(src/'phases.log').read_text()
    run(resume=True)
    assert len(packages)==1
    assert (src/'phases.log').read_text()==phases


def test_changed_recipe_refused_without_deleting_objects(pipeline,monkeypatch):
    _,recipe,src,_,run,_=pipeline
    monkeypatch.setenv('FAIL_BUILD','1')
    with pytest.raises(SystemExit): run()
    recipe.write_text(recipe.read_text()+'\n# changed\n')
    with pytest.raises(BuildStateError,match='Recipe'): run(resume=True)
    assert (src/'resume-demo-1/objects/first.o').is_file()


def test_changed_local_source_and_missing_tree_refused(pipeline,monkeypatch):
    _,recipe,src,_,run,_=pipeline
    monkeypatch.setenv('FAIL_BUILD','1')
    with pytest.raises(SystemExit): run()
    patch=recipe.parent/'fix.patch'; old=patch.read_text(); patch.write_text(old+'\n')
    with pytest.raises(BuildStateError,match='local sources'): run(resume=True)
    patch.write_text(old)
    shutil.rmtree(src)
    with pytest.raises(BuildStateError,match='missing'): run(resume=True)


def test_resume_never_creates_missing_state(pipeline):
    _,_,src,_,run,_=pipeline
    with pytest.raises(BuildStateError,match='No tracked'): run(resume=True)
    assert not src.exists()


def test_restart_is_explicitly_destructive(pipeline,monkeypatch):
    _,_,src,_,run,_=pipeline
    monkeypatch.setenv('FAIL_BUILD','1')
    with pytest.raises(SystemExit): run()
    marker=src/'old.marker'; marker.write_text('old')
    monkeypatch.delenv('FAIL_BUILD')
    run(restart=True)
    assert not marker.exists()


def test_legacy_recipe_resume_flag_fails_before_cleanup(pipeline,monkeypatch):
    _,_,src,_,run,_=pipeline
    monkeypatch.setenv('KMAKE_RESUME','1')
    with pytest.raises(BuildStateError,match='recipe-only'): run()
    assert not src.exists()


def test_flags_changed_refuses_resume_and_preserves_objects(pipeline,monkeypatch):
    _,_,src,_,run,_=pipeline
    monkeypatch.setenv('FAIL_BUILD','1')
    with pytest.raises(SystemExit): run()
    monkeypatch.setenv('CFLAGS','-O1 -DCHANGED')
    with pytest.raises(BuildStateError,match='compiler changed'): run(resume=True)
    assert (src/'resume-demo-1/objects/first.o').exists()


def test_session_lock_prevents_same_package_parallel_cleanup(tmp_path):
    identity={'name':'demo'}
    with BuildSession(tmp_path,identity):
        with pytest.raises(BuildStateError,match='already running'):
            with BuildSession(tmp_path,identity): pass


def test_buildpkg_kmake_resume_keeps_kernel_objects_and_session(pipeline,monkeypatch,tmp_path):
    from test_kmake_unittest import KmakeTests
    app,recipe,src,state,run,_=pipeline
    fixture=KmakeTests(); fixture.setUp()
    try:
        # Re-establish this pipeline's environment after the compiler fixture.
        monkeypatch.setenv('PYTHONPATH',str(Path(app.__file__).resolve().parents[1])+os.pathsep+'/tmp/lpm-resume-test-deps')
        monkeypatch.setenv('LPM_COMPILER_CACHE','none')
        config=recipe.parent/'kernel.config'; config.write_text(fixture.config.read_text())
        (fixture.source/'COPYING').write_text('license')
        archive=recipe.parent/'kernel.tar.gz'
        with tarfile.open(archive,'w:gz') as tf:
            tf.add(fixture.source,arcname='linux-7.2.11')
        recipe.write_text('''NAME=resume-demo
VERSION=7.2.11
RELEASE=2
ARCH=x86_64
SOURCE=("kernel.tar.gz" "kernel.config")
BUILD_OPTIONS=()
_kernel() {
    python3 -m lpm.kmake --source "$SRCROOT/$NAME-$VERSION" \\
        --localversion=-lpm2 --cache none "$@"
}
prepare() { _kernel configure --config "$SRCROOT/kernel.config"; }
build() { _kernel build; }
check() { _kernel check; }
staging() { _kernel stage --destdir "$pkgdir" --pkgbase "$NAME"; }
''')
        monkeypatch.setenv('TEST_FAIL_BUILD','1')
        with pytest.raises(SystemExit): run()
        output=src/'resume-demo-7.2.11/_build'
        before=(output/'reused.o').stat().st_ino
        token=json.loads(state.read_text())['token']
        assert json.loads((output/'.kmake.json').read_text())['lpm_build_session']==token
        monkeypatch.delenv('TEST_FAIL_BUILD')
        run(resume=True)
        assert (output/'reused.o').stat().st_ino==before
        commands=[json.loads(line)['args'] for line in (output/'commands.jsonl').read_text().splitlines()]
        assert sum('olddefconfig' in c for c in commands)==1
        assert sum('bzImage' in c for c in commands)==2
        assert json.loads((output/'.kmake.json').read_text())['built']
    finally:
        fixture.doCleanups()


def test_cli_resume_real_compilation_and_packaging(tmp_path):
    import subprocess
    import sys
    if not shutil.which('cc') or not shutil.which('make') or not shutil.which('tar'):
        pytest.skip('requires native C build tools')
    source=tmp_path/'tiny-1'; source.mkdir()
    (source/'first.c').write_text('int first(void) { return 5; }\n')
    (source/'second.c').write_text('int first(void); int main(void) { return first() != 5; }\n')
    (source/'Makefile').write_text('''all: demo
first.o: first.c
\t$(CC) -c first.c -o first.o
second.o: second.c first.o
\twhile test -e "$${PAUSE_FILE:-/nonexistent}"; do sleep 0.05; done
\ttest "$${FAIL_BUILD:-0}" != 1
\t$(CC) -c second.c -o second.o
demo: first.o second.o
\t$(CC) first.o second.o -o demo
''')
    archive=tmp_path/'tiny.tar.gz'
    with tarfile.open(archive,'w:gz') as tf: tf.add(source,arcname='tiny-1')
    recipe=tmp_path/'tiny.lpmbuild'
    recipe.write_text('''NAME=resume-native
VERSION=1
ARCH=x86_64
SOURCE=("tiny.tar.gz")
BUILD_OPTIONS=()
prepare() { test -f "$SRCROOT/$NAME-$VERSION/Makefile"; }
build() { make -C "$SRCROOT/$NAME-$VERSION" -j1; }
check() { "$SRCROOT/$NAME-$VERSION/demo"; }
staging() { mkdir -p "$pkgdir/usr/bin"; cp "$SRCROOT/$NAME-$VERSION/demo" "$pkgdir/usr/bin/demo"; }
''')
    env=dict(os.environ,LPM_STATE_DIR=str(tmp_path/'state'),LPM_BUILD_WORKDIR=str(tmp_path/'work'),
             LPM_COMPILER_CACHE='none')
    pause=tmp_path/'pause'; pause.touch(); env['PAUSE_FILE']=str(pause)
    root=Path(__file__).resolve().parents[1]
    env['PYTHONPATH']=str(root/'src')+os.pathsep+os.environ.get('PYTHONPATH','')
    # Exercise the real CLI and real package writer without signing with host keys.
    entry='from pathlib import Path; from lpm import app; app.SIGN_KEY=Path("/tmp/lpm-resume-test-no-signing-key"); app.main()'
    args=[sys.executable,'-c',entry,'buildpkg',str(recipe),'--no-deps','--install-default','n']
    import signal
    import time
    object=tmp_path/'work/resume-native/src/resume-native-1/first.o'
    log=tmp_path/'interrupted.log'
    with log.open('w') as output:
        process=subprocess.Popen(args,env=env,cwd=tmp_path,text=True,
                                 stdout=output,stderr=output,stdin=subprocess.DEVNULL,
                                 start_new_session=True)
        try:
            deadline=time.monotonic()+15
            while not object.is_file() and process.poll() is None and time.monotonic()<deadline:
                time.sleep(.05)
            assert object.is_file(),log.read_text()
            before=object.stat().st_mtime_ns
        finally:
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM)
            process.wait(timeout=20)
    assert process.returncode!=0
    pause.unlink()
    resumed=subprocess.run([*args,'--resume'],env=env,cwd=tmp_path,text=True,capture_output=True,input='n\n',timeout=60)
    assert resumed.returncode==0,resumed.stdout+resumed.stderr
    assert object.stat().st_mtime_ns==before
    assert (tmp_path/'resume-native-1-1.x86_64.zst').is_file()
    status=subprocess.run([sys.executable,'-c',entry,'buildpkg',str(recipe),'--status'],env=env,cwd=tmp_path,text=True,capture_output=True,timeout=30)
    assert status.returncode==0,status.stderr
    assert json.loads(status.stdout)['status']=='packaged'


def test_dependency_resume_uses_its_own_checkpoint(pipeline,monkeypatch,tmp_path):
    app,recipe,src,state,_,_=pipeline
    child=tmp_path/'child.lpmbuild'
    child.write_text('''NAME=resume-child
VERSION=1
ARCH=noarch
SOURCE=()
prepare() { printf 'prepare\\n' >> "$SRCROOT/phases.log"; }
build() {
    printf 'build\\n' >> "$SRCROOT/phases.log"
    [ -f "$SRCROOT/object.o" ] || echo compiled > "$SRCROOT/object.o"
    [ "${FAIL_BUILD:-0}" != 1 ]
}
staging() { echo dependency > "$pkgdir/payload"; }
''')
    recipe.write_text(recipe.read_text().replace('REQUIRES=()', 'REQUIRES=(resume-child)'))
    class Conn:
        def close(self): pass
    monkeypatch.setattr(app,'db',lambda:Conn())
    monkeypatch.setattr(app,'db_installed',lambda conn:{})
    built={}
    def package(root,meta,out,sign=True):
        out.write_bytes(b'package'); built[out.resolve()]=meta
    monkeypatch.setattr(app,'build_package',package)
    monkeypatch.setattr(app,'read_package_meta',lambda p:(built[p.resolve()],[]))
    def fetch(name,dest):
        assert name=='resume-child'
        shutil.copyfile(child,dest)
        return dest
    def run(**kwargs):
        return app.run_lpmbuild(recipe,tmp_path,build_deps=True,prompt_install=False,fetcher=fetch,**kwargs)
    monkeypatch.setenv('FAIL_BUILD','1')
    with pytest.raises(SystemExit): run()
    childsrc=tmp_path/'work/resume-child/src'
    object=childsrc/'object.o'; before=object.stat().st_mtime_ns
    monkeypatch.delenv('FAIL_BUILD')
    run(resume=True)
    assert object.stat().st_mtime_ns==before
    assert (childsrc/'phases.log').read_text().splitlines()==['prepare','build','build']


def test_state_refuses_workspace_path_tampering(pipeline,monkeypatch):
    _,_,_,state,run,_=pipeline
    monkeypatch.setenv('FAIL_BUILD','1')
    with pytest.raises(SystemExit): run()
    data=json.loads(state.read_text())
    root=Path('/'); st=root.stat()
    data['directories']['pkg']={'path':'/','device':st.st_dev,'inode':st.st_ino}
    state.write_text(json.dumps(data))
    with pytest.raises(BuildStateError,match='workspace path'): run(resume=True)


def test_install_retry_does_not_rebuild_package(pipeline,monkeypatch,tmp_path):
    app,recipe,src,state,_,packages=pipeline
    def fail_install(*args,**kwargs):
        raise RuntimeError('installation failed')
    monkeypatch.setattr(app,'prompt_install_pkg',fail_install)
    def run(**kwargs):
        return app.run_lpmbuild(recipe,tmp_path,build_deps=False,prompt_install=True,**kwargs)
    with pytest.raises(RuntimeError,match='installation failed'): run()
    assert json.loads(state.read_text())['status']=='packaged'
    before=(src/'phases.log').read_text()
    monkeypatch.setattr(app,'prompt_install_pkg',lambda *a,**kw:None)
    run(resume=True)
    assert len(packages)==1
    assert (src/'phases.log').read_text()==before


def test_deferred_queue_can_resume_matching_checkpoint(pipeline,monkeypatch):
    _,_,src,_,run,_=pipeline
    monkeypatch.setenv('FAIL_BUILD','1')
    with pytest.raises(SystemExit): run()
    monkeypatch.delenv('FAIL_BUILD')
    run(_resume_available=True)
    assert (src/'phases.log').read_text().splitlines()==['prepare','build','build','check','staging']


def test_resume_recovers_missing_disposable_staging_directory(pipeline,monkeypatch):
    _,_,src,state,run,_=pipeline
    monkeypatch.setenv('FAIL_STAGE','1')
    with pytest.raises(SystemExit): run()
    pkg=Path(json.loads(state.read_text())['directories']['pkg']['path'])
    shutil.rmtree(pkg)
    monkeypatch.delenv('FAIL_STAGE')
    run(resume=True)
    assert (pkg/'usr/bin/demo').is_file()
    assert (src/'phases.log').read_text().splitlines().count('build')==1


def test_incomplete_source_phase_requires_explicit_restart(pipeline,monkeypatch):
    app,_,src,_,run,_=pipeline
    original=app._apply_lpmbuild_patches
    monkeypatch.setattr(app,'_apply_lpmbuild_patches',lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('source interrupted')))
    with pytest.raises(RuntimeError,match='source interrupted'): run()
    marker=src/'keep.marker'; marker.write_text('keep')
    with pytest.raises(BuildStateError,match='Source preparation'): run(resume=True)
    assert marker.is_file()
    monkeypatch.setattr(app,'_apply_lpmbuild_patches',original)
    run(restart=True)
    assert not marker.exists()


def test_final_stage_resume_preserves_phase_selection(pipeline,monkeypatch):
    _,recipe,src,_,run,_=pipeline
    recipe.write_text(recipe.read_text()+'\nbuild_stage2() { build; }\n')
    monkeypatch.setenv('FAIL_BUILD','1')
    with pytest.raises(SystemExit): run(_build_stage=2)
    monkeypatch.delenv('FAIL_BUILD')
    run(_build_stage=2,_resume_available=True)
    assert (src/'phases.log').read_text().splitlines()==['prepare','build','build','check','staging']
