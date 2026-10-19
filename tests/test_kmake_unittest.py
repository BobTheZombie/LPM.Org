import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lpm import kmake

REQUIRED = ('MODULES','BLK_DEV_INITRD','RD_ZSTD','DEVTMPFS','PROC_FS','SYSFS','TMPFS','CGROUPS')


class KmakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root/'linux'
        self.source.mkdir()
        (self.source/'Makefile').write_text('VERSION = 7\nPATCHLEVEL = 2\nSUBLEVEL = 11\n')
        (self.source/'scripts').mkdir()
        (self.source/'scripts/config').write_text('#!/bin/bash\nexit 0\n')
        self.config = self.root/'full.config'
        self.config.write_text(''.join(f'CONFIG_{s}=y\n' for s in REQUIRED))
        self.output = self.root/'output'
        self.bin = self.root/'bin'
        self.bin.mkdir()
        self.program('gcc', '#!/bin/sh\nprintf "%s\\n" "${TEST_COMPILER_VERSION:-test-gcc-1}"\n')
        self.program('make', '''#!/usr/bin/env python3
import json, os
from pathlib import Path
import sys
args=sys.argv[1:]
v=dict(arg.split('=',1) for arg in args if '=' in arg)
o=Path(v['O']); o.mkdir(exist_ok=True)
with (o/'commands.jsonl').open('a') as f:
 f.write(json.dumps({'args':args,'CFLAGS':os.environ.get('CFLAGS')})+'\\n')
release='7.2.11'+v.get('LOCALVERSION','')
if 'kernelrelease' in args:
 print(release)
elif 'bzImage' in args or 'Image' in args:
 (o/'reused.o').write_text('completed object')
 if os.environ.get('TEST_FAIL_BUILD')=='1': sys.exit(2)
 image=o/('arch/arm64/boot/Image' if v['ARCH']=='arm64' else 'arch/x86/boot/bzImage')
 image.parent.mkdir(parents=True,exist_ok=True)
 h=bytearray(0x206); h[:2]=b'MZ'; h[0x202:0x206]=b'HdrS'; h[0x38:0x3c]=b'ARM\\x64'; image.write_bytes(h)
 (o/'include/config').mkdir(parents=True,exist_ok=True)
 (o/'include/config/kernel.release').write_text(release)
 for name in ('System.map','modules.order','modules.builtin'): (o/name).write_text('test\\n')
elif 'modules_install' in args:
 m=Path(v['INSTALL_MOD_PATH'])/'lib/modules'/release
 m.mkdir(parents=True,exist_ok=True)
 (m/'build').symlink_to(o)
 (m/'kernel.ko').write_bytes(b'module')
''')
        self.program('depmod', '''#!/usr/bin/env python3
from pathlib import Path
import sys
args=sys.argv[1:]; base=Path(args[args.index('-b')+1])
(base/'lib/modules'/args[-1]/'modules.dep').write_text('kernel.ko:\\n')
''')
        self.env = patch.dict(os.environ, {'PATH':str(self.bin)+os.pathsep+os.environ['PATH'],
                                          'CFLAGS':'-march=znver2 -O3', 'LPM_BUILD_JOBS':'2'})
        self.env.start()
        self.addCleanup(self.env.stop)
        for name in ('CC','HOSTCC','HOSTCXX','CROSS_COMPILE','LLVM','LLVM_IAS','LPM_TARGETS_FILE'):
            os.environ.pop(name, None)

    def program(self, name, text):
        path = self.bin/name; path.write_text(text); path.chmod(0o755)

    def run_cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return kmake.main(['--source',str(self.source),'--output',str(self.output),
                               '--arch','x86_64','--cache','none',*args])

    def configure(self):
        self.assertEqual(self.run_cli('--localversion=-lpm2','configure','--config',str(self.config)),0)

    def test_configure_build_check_stage(self):
        self.configure()
        self.assertEqual(self.run_cli('build'),0)
        self.assertEqual(self.run_cli('check'),0)
        self.assertEqual(self.run_cli('stage','--destdir',str(self.root/'pkg')),0)
        module=self.root/'pkg/usr/lib/modules/7.2.11-lpm2'
        self.assertTrue((module/'vmlinuz').is_file())
        self.assertTrue((module/'modules.dep').is_file())
        self.assertFalse((module/'build').exists())
        state=json.loads((self.output/'.kmake.json').read_text())
        self.assertTrue(state['built'] and state['staged'])
        commands=[json.loads(line) for line in (self.output/'commands.jsonl').read_text().splitlines()]
        self.assertTrue(all(c['CFLAGS'] is None for c in commands))
        self.assertTrue(any('-j2' in c['args'] for c in commands))
        self.assertFalse((self.root/'pkg/boot').exists())

    def test_failure_keeps_objects_and_resumes(self):
        self.configure()
        with patch.dict(os.environ, {'TEST_FAIL_BUILD':'1'}):
            self.assertEqual(self.run_cli('build'),1)
        self.assertTrue((self.output/'reused.o').is_file())
        self.assertFalse(json.loads((self.output/'.kmake.json').read_text())['built'])
        self.assertEqual(self.run_cli('stage','--destdir',str(self.root/'pkg')),1)
        self.assertEqual(self.run_cli('build'),0)

    def test_config_drift_requires_configure(self):
        self.configure()
        with (self.output/'.config').open('a') as f: f.write('CONFIG_NEW=y\n')
        self.assertEqual(self.run_cli('build'),1)
        self.assertEqual(self.run_cli('configure'),0)
        self.assertIn('CONFIG_NEW=y',(self.output/'.config').read_text())
        self.assertEqual(self.run_cli('build'),0)

    def test_toolchain_drift(self):
        self.configure()
        with patch.dict(os.environ, {'TEST_COMPILER_VERSION':'test-gcc-2'}):
            self.assertEqual(self.run_cli('build'),1)

    def test_staging_refuses_root_and_symlinks(self):
        self.configure(); self.assertEqual(self.run_cli('build'),0)
        self.assertEqual(self.run_cli('stage','--destdir','/'),1)
        pkg=self.root/'pkg'; pkg.mkdir(); (pkg/'usr').symlink_to(self.root/'outside')
        self.assertEqual(self.run_cli('stage','--destdir',str(pkg)),1)

    def test_missing_baseline_and_required_symbol(self):
        self.assertEqual(self.run_cli('configure'),1)
        self.config.write_text('CONFIG_MODULES=y\n')
        self.assertEqual(self.run_cli('configure','--config',str(self.config)),1)

    def test_resources(self):
        with patch.object(kmake,'cpu_count',return_value=16), patch.object(kmake,'available_memory_mb',return_value=8192):
            self.assertEqual(kmake.resource_jobs(),4)
            self.assertEqual(kmake.resource_jobs(10),10)
        with patch.object(kmake,'cpu_count',return_value=2), patch.object(kmake,'available_memory_mb',return_value=128):
            self.assertEqual(kmake.resource_jobs(),1)

    def test_cache_wraps_compiler_without_relaxed_settings(self):
        self.program('ccache','#!/bin/sh\nexec "$@"\n')
        env, selected=kmake.build_environment('ccache', self.root/'cache')
        self.assertTrue(selected.endswith('/ccache'))
        self.assertIn('ccache', env['CC'])
        self.assertEqual(env['CCACHE_COMPILERCHECK'],'content')
        self.assertNotIn('CFLAGS',env)

    def test_conflicting_baselines(self):
        self.assertEqual(self.run_cli('configure','--config',str(self.config),'--defconfig'),1)

    def test_staging_rejects_stale_module_tree(self):
        self.configure(); self.assertEqual(self.run_cli('build'),0)
        pkg=self.root/'pkg'
        self.assertEqual(self.run_cli('stage','--destdir',str(pkg)),0)
        self.assertEqual(self.run_cli('stage','--destdir',str(pkg)),1)

    def test_requested_cache_is_required(self):
        with patch.object(kmake.shutil, 'which', return_value=None):
            with self.assertRaises(RuntimeError):
                kmake.build_environment('ccache')

    def test_other_architecture_and_invalid_image(self):
        self.assertEqual(self.run_cli('--arch','arm64','configure','--config',str(self.config)),0)
        self.assertEqual(self.run_cli('--arch','arm64','build'),0)
        image=self.output/'arch/arm64/boot/Image'
        image.write_bytes(b'invalid')
        self.assertEqual(self.run_cli('--arch','arm64','check'),1)

    def test_llvm_is_explicitly_unsupported(self):
        with patch.dict(os.environ, {'LLVM':'1'}):
            self.assertEqual(self.run_cli('configure','--config',str(self.config)),1)

    def test_cache_wrapper_executes_compiler(self):
        self.program('ccache', '#!/bin/sh\nexec "$@"\n')
        env, _ = kmake.build_environment('ccache',self.root/'cache')
        import shlex, subprocess
        output=subprocess.check_output([*shlex.split(env['CC']), '--version'],env=env,text=True)
        self.assertIn('test-gcc-1',output)

    def test_explicit_resume_and_session_mismatch(self):
        with patch.dict(os.environ, {'LPM_BUILD_SESSION':'one'}):
            self.configure()
            with patch.dict(os.environ, {'TEST_FAIL_BUILD':'1'}):
                self.assertEqual(self.run_cli('build'),1)
            self.assertEqual(self.run_cli('resume'),0)
        with patch.dict(os.environ, {'LPM_BUILD_SESSION':'two'}):
            self.assertEqual(self.run_cli('resume'),1)
        self.assertTrue((self.output/'reused.o').is_file())

    def test_resume_does_not_configure_missing_state(self):
        self.assertEqual(self.run_cli('resume'),1)
        self.assertFalse((self.output/'.config').exists())


if __name__ == '__main__':
    unittest.main()
