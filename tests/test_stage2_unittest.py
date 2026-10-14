"""Run with PYTHONPATH=src python -m unittest discover -s tests -p '*stage2_unittest.py'."""
import dataclasses
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from lpm import app, stage2


class Stage2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.script = self.root / 'bootstrap.lpmbuild'
        self.script.write_text('NAME=bootstrap\nVERSION=1\nRELEASE=1\nARCH=noarch\n'
                               'STAGE_2_REQUIRES=(introspection)\n'
                               'prepare() { :; }\nbuild() { :; }\nstaging() { :; }\n'
                               'build_stage2() { :; }\n')
        self.meta = app.PkgMeta('bootstrap', '1', arch='noarch', build_stage=1)
        self.current = {'bootstrap': dataclasses.asdict(self.meta)}
        stage2.enqueue(self.root, self.script, self.root, self.meta, ['introspection'])
        self.calls = []

    def fetch(self, name, dest):
        dest.write_text(name)
        return dest

    def build(self, script, outdir, stage, overrides):
        name = 'bootstrap' if stage == 2 else script.read_text()
        self.calls.append((name, stage))
        self.current[name] = dataclasses.asdict(app.PkgMeta(name, '1', arch='noarch', build_stage=2))
        return (outdir / name, 0, 3, [])

    def drain(self, build=None):
        return stage2.drain(self.root, lambda: self.current, build or self.build, self.fetch)

    def test_order_and_no_repeat(self):
        result = self.drain()
        self.assertIn('bootstrap', result)
        self.assertEqual(self.calls, [('introspection', 1), ('bootstrap', 2)])
        self.assertEqual(list((self.root / 'stage2').glob('*.json')), [])
        self.drain()
        self.assertEqual(len(self.calls), 2)

    def test_declined_dependency_remains_pending_and_resumes(self):
        def decline(script, outdir, stage, overrides):
            self.calls.append(('declined', stage))
        self.drain(decline)
        self.assertEqual(self.calls, [('declined', 1)])
        self.assertEqual(len(list((self.root / 'stage2').glob('*.json'))), 1)
        self.drain()
        self.assertEqual(self.current['bootstrap']['build_stage'], 2)

    def test_declined_bootstrap_does_not_build_dependencies(self):
        self.current.clear()
        self.drain()
        self.assertEqual(self.calls, [])

    def test_failed_final_retains_job(self):
        def fail(script, outdir, stage, overrides):
            if stage == 2:
                raise RuntimeError('build failed')
            return self.build(script, outdir, stage, overrides)
        with self.assertRaisesRegex(RuntimeError, 'build failed'):
            self.drain(fail)
        self.assertEqual(len(list((self.root / 'stage2').glob('*.json'))), 1)
        self.drain()

    def test_declined_final_remains_pending(self):
        def decline_final(script, outdir, stage, overrides):
            if stage == 2:
                self.calls.append(('final-declined', 2))
                return (outdir / 'final', 0, 3, [])
            return self.build(script, outdir, stage, overrides)
        self.drain(decline_final)
        self.assertEqual(self.current['bootstrap']['build_stage'], 1)
        self.assertEqual(len(list((self.root / 'stage2').glob('*.json'))), 1)
        self.drain()
        self.assertEqual(self.current['bootstrap']['build_stage'], 2)

    def test_scheduler_lock_prevents_duplicate_builds(self):
        import fcntl
        with (self.root / 'stage2' / 'scheduler.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.drain(), {})
            self.assertEqual(self.calls, [])
        self.drain()
        self.assertEqual(len(self.calls), 2)

    def test_nested_jobs_are_drained_without_reentry(self):
        nested_script = self.root / 'nested.lpmbuild'
        nested_script.write_text('nested recipe')
        def build_nested(script, outdir, stage, overrides):
            if stage == 1:
                self.current['introspection'] = dataclasses.asdict(
                    app.PkgMeta('introspection', '1', arch='noarch', build_stage=1))
                stage2.enqueue(self.root, nested_script, outdir,
                    app.PkgMeta('introspection', '1', arch='noarch', build_stage=1),
                    ['bootstrap'])
                return (outdir / 'dep', 0, 3, [])
            if script == nested_script:
                self.current['introspection']['build_stage'] = 2
                return (outdir / 'dep-final', 0, 3, [])
            return self.build(script, outdir, stage, overrides)
        result = self.drain(build_nested)
        self.assertEqual(set(result), {'bootstrap', 'introspection'})
        self.assertEqual(list((self.root / 'stage2').glob('*.json')), [])

    def test_changed_recipe_is_rejected(self):
        self.script.write_text(self.script.read_text() + '# changed\n')
        with self.assertRaisesRegex(RuntimeError, 'changed or is missing'):
            self.drain()
        self.assertEqual(self.calls, [])

    def test_installed_provider_skips_fetch(self):
        self.current['other'] = dict(provides=['introspection=1'])
        self.drain()
        self.assertEqual(self.calls, [('bootstrap', 2)])

    def test_final_marker_roundtrip_and_existing_metadata(self):
        self.assertEqual(app.PkgMeta.from_dict(dataclasses.asdict(self.meta)).build_stage, 1)
        self.assertEqual(app.PkgMeta.from_dict(dict(name='old', version='1')).build_stage, 2)

    def test_dependency_validation(self):
        for name in ['foo>=1', '../foo', 'foo/bar', '']:
            with self.assertRaises(ValueError):
                stage2.validate_dependencies([name])

    def test_metadata_capture(self):
        _, arrays, _ = app._capture_lpmbuild_metadata(self.script)
        self.assertEqual(arrays['STAGE_2_REQUIRES'], ['introspection'])

    def test_stage_specific_runtime_metadata(self):
        self.script.write_text(self.script.read_text() + '\nif [ "${LPM_BUILD_STAGE:-1}" = 2 ]; then REQUIRES=(pam util-linux-libs); fi\n')
        _, initial, _ = app._capture_lpmbuild_metadata(self.script, build_stage=1)
        _, final, _ = app._capture_lpmbuild_metadata(self.script, build_stage=2)
        self.assertEqual(initial['REQUIRES'], [])
        self.assertEqual(final['REQUIRES'], ['pam', 'util-linux-libs'])

    def strict_setup(self, cycle=False):
        self.script.write_text(self.script.read_text() + '\nSTAGE_2_BUILD_REQUIRES=(finaldep)\n')
        stage2.enqueue(self.root, self.script, self.root, self.meta,
                       ['finaldep'], final_dependencies=['finaldep'])
        def fetch(name, dest):
            dest.write_text('NAME=finaldep\nVERSION=1\nRELEASE=1\nARCH=noarch\n'
                'STAGE_2_REQUIRES=(bootstrap)\nbuild_stage2() { :; }\n' +
                ('STAGE_2_BUILD_REQUIRES=(bootstrap)\n' if cycle else ''))
        def build(script, outdir, stage, overrides):
            scal, _, _ = app._capture_lpmbuild_metadata(script)
            name = scal['NAME']
            self.calls.append((name, stage))
            self.current[name] = dataclasses.asdict(app.PkgMeta(name, '1', arch='noarch', build_stage=stage))
            return name
        return fetch, build

    def test_strict_recursive_order(self):
        fetch, build = self.strict_setup()
        result = stage2.drain(self.root, lambda: self.current, build, fetch,
                              read_recipe=app._capture_lpmbuild_metadata)
        self.assertEqual(self.calls, [('finaldep', 1), ('finaldep', 2), ('bootstrap', 2)])
        self.assertEqual(set(result), {'finaldep', 'bootstrap'})

    def test_strict_cycle_before_any_build(self):
        fetch, build = self.strict_setup(cycle=True)
        with self.assertRaisesRegex(RuntimeError, 'bootstrap@2 -> finaldep@2 -> bootstrap@2'):
            stage2.drain(self.root, lambda: self.current, build, fetch,
                         read_recipe=app._capture_lpmbuild_metadata)
        self.assertEqual(self.calls, [])

    def test_plan_exports_graph_without_building(self):
        fetch, build = self.strict_setup()
        result = stage2.drain(self.root, lambda: self.current, build, fetch,
                              read_recipe=app._capture_lpmbuild_metadata, plan_only=True)
        self.assertEqual(result['order'], ['finaldep@2', 'bootstrap@2'])
        self.assertEqual(len(result['edges']), 1)
        self.assertEqual(result['layers'], [['finaldep@2'], ['bootstrap@2']])
        self.assertEqual(self.calls, [])

    def test_declined_strict_final_blocks_dependent_and_resumes(self):
        fetch, build = self.strict_setup()
        def decline(script, outdir, stage, overrides):
            if script.name == 'finaldep.lpmbuild' and stage == 2:
                return None
            return build(script, outdir, stage, overrides)
        stage2.drain(self.root, lambda: self.current, decline, fetch,
                     read_recipe=app._capture_lpmbuild_metadata)
        self.assertEqual(self.current['bootstrap']['build_stage'], 1)
        stage2.drain(self.root, lambda: self.current, build, fetch,
                     read_recipe=app._capture_lpmbuild_metadata)
        self.assertEqual(self.current['bootstrap']['build_stage'], 2)

    def test_dynamic_dependency_completion_revisits_blocked_parent(self):
        self.current['introspection'] = dict(name='introspection', version='1', release='1', arch='noarch', build_stage=1)
        self.current['extra'] = dict(name='extra', version='1', release='1', arch='noarch', build_stage=1)
        extra = self.root/'extra.lpmbuild'
        extra.write_text('NAME=extra\nVERSION=1\nARCH=noarch\nSTAGE_2_REQUIRES=(introspection)\nbuild_stage2() { :; }\n')
        stage2.enqueue(self.root, extra, self.root,
                       app.PkgMeta('extra', '1', arch='noarch', build_stage=1), ['introspection'])
        # The first install attempt is declined, but another completed final
        # build makes the dependency available within this scheduler run.
        self.current.pop('introspection')
        finals = []
        def fetch(name, dest):
            dest.write_text(name)
        def build(script, outdir, stage, overrides):
            if stage == 2:
                name = 'extra' if script == extra else 'bootstrap'
                finals.append(name)
                self.current[name]['build_stage'] = 2
                self.current['introspection'] = dict(name='introspection', provides=[])
            elif finals or script.name == 'introspection.lpmbuild':
                # First attempt stays unavailable; second makes it ready.
                if getattr(build, 'attempted', False):
                    self.current['introspection'] = dict(name='introspection', provides=[])
                build.attempted = True
        stage2.drain(self.root, lambda: self.current, build, fetch,
                     read_recipe=app._capture_lpmbuild_metadata)
        self.assertEqual(finals, ['extra', 'bootstrap'])

    def test_actual_recipe_cycle_and_phase_fallback(self):
        # Execute shell phases for real; stub only packaging, installation and DB.
        self.current.clear()
        phases = []
        blobs = {}
        self.script.write_text('NAME=lpmstage2test\nVERSION=1\nRELEASE=1\nARCH=noarch\n'
            'STAGE_2_REQUIRES=(lpmstage2dep)\n'
            'prepare() { test ! -f "$SRCROOT/dirty"; touch "$SRCROOT/dirty"; }\n'
            'build() { test "$LPM_BUILD_STAGE" = 1; }\n'
            'build_stage2() { test "$LPM_BUILD_STAGE" = 2; }\n'
            'staging() { mkdir -p "$pkgdir/usr/share"; echo "$LPM_BUILD_STAGE" > "$pkgdir/usr/share/stage"; }\n')
        def fetch(name, dest):
            dest.write_text('NAME=lpmstage2dep\nVERSION=1\nARCH=noarch\n'
                           'BUILD_REQUIRES=(lpmstage2test)\n'
                           'build() { :; }\nstaging() { :; }\n')
            return dest
        def shell(func, cwd, env, script, stagedir, buildroot, srcroot, aliases=()):
            phases.append((script.name, func, env['LPM_BUILD_STAGE']))
            names = ' '.join((func, *aliases))
            subprocess.run(['bash', '-e', '-c', 'source "$1"; for f in '+names+
                            '; do if declare -F "$f" >/dev/null; then "$f"; break; fi; done',
                            'bash', str(script)], cwd=cwd, env=env, check=True)
        def package(stagedir, meta, out, sign=True):
            out.write_text('test package')
            blobs[out] = dataclasses.asdict(meta)
        def install(out, **kwargs):
            self.current[blobs[out]['name']] = blobs[out]
        class Conn:
            def close(self): pass
        with patch.dict(os.environ, LPM_STATE_DIR=str(self.root / 'integration-state')), \
             patch.object(app, 'db', return_value=Conn()), \
             patch.object(app, 'db_installed', side_effect=lambda conn: self.current.copy()), \
             patch.object(app, 'sandboxed_run', side_effect=shell), \
             patch.object(app, 'build_package', side_effect=package), \
             patch.object(app, 'prompt_install_pkg', side_effect=install), \
             patch.object(app, 'generate_install_script', return_value='true'):
            out, _, _, _ = app.run_lpmbuild(self.script, self.root, fetcher=fetch)
        self.assertEqual(blobs[out]['build_stage'], 2)
        self.assertEqual([p[1] for p in phases], ['prepare', 'build', 'staging',
                         'prepare', 'build', 'staging',
                         'prepare_stage2', 'build_stage2', 'staging_stage2'])
        self.assertEqual(self.current['lpmstage2test']['build_stage'], 2)
        self.assertEqual(Path('/tmp/pkg-lpmstage2test/usr/share/stage').read_text().strip(), '2')


if __name__ == '__main__':
    unittest.main()
