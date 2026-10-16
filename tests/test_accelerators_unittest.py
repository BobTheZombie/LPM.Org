import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from lpm import accelerators as accel, app


class AcceleratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {'PATH': os.environ.get('PATH',''), 'LPM_COMPILER_CACHE':'none'}

    def configure(self, env=None, **kwargs):
        with patch.object(accel,'cpu_count',return_value=16), patch.object(accel,'available_memory_mb',return_value=8192):
            return accel.configure(dict(self.env if env is None else env), self.root/'build',self.root/'state',**kwargs)

    def test_memory_budget_profiles(self):
        generic=self.configure()
        qt=self.configure(profile='qt')
        self.assertEqual(generic['LPM_BUILD_JOBS'],'8')
        self.assertEqual(qt['LPM_BUILD_JOBS'],'4')
        self.assertEqual(qt['LPM_LINK_JOBS'],'1')
        self.assertIn('lpm_link=1', (self.root/'build/accelerators/job-pools.cmake').read_text())

    def test_explicit_jobs_and_toolchain_preserved(self):
        env=self.configure({**self.env,'LPM_BUILD_JOBS':'12','MAKEFLAGS':'-j3',
                            'CMAKE_BUILD_PARALLEL_LEVEL':'3','CMAKE_TOOLCHAIN_FILE':'custom.cmake'})
        self.assertEqual(env['LPM_BUILD_JOBS'],'12')
        self.assertEqual(env['MAKEFLAGS'],'-j3')
        self.assertEqual(env['CMAKE_BUILD_PARALLEL_LEVEL'],'3')
        self.assertEqual(env['CMAKE_TOOLCHAIN_FILE'],'custom.cmake')

    def test_missing_explicit_backend_is_error(self):
        with patch.object(accel.shutil,'which',return_value=None):
            with self.assertRaisesRegex(RuntimeError,'not installed'):
                self.configure({**self.env,'LPM_COMPILER_CACHE':'ccache'})
            self.assertEqual(self.configure({**self.env,'LPM_COMPILER_CACHE':'auto'})['LPM_COMPILER_CACHE_ACTIVE'],'none')

    def test_invalid_options(self):
        for key,value in [('LPM_BUILD_PROFILE','fastest'),('LPM_COMPILER_CACHE','oops'),
                          ('LPM_MEMORY_PER_JOB_MB','0'),('LPM_BUILD_JOBS','0')]:
            with self.assertRaises(ValueError):
                self.configure({**self.env,key:value})

    def fake_cache(self):
        binary=self.root/'ccache'
        binary.write_text('#!/bin/sh\nprintf "cache-called\\n" >> "$CCACHE_DIR/calls"\nexec "$@"\n')
        binary.chmod(0o755)
        return binary

    def test_real_compiler_through_cache_wrapper(self):
        compiler=shutil.which('gcc')
        if not compiler:
            self.skipTest('gcc unavailable')
        backend=self.fake_cache()
        real_which=shutil.which
        def which(name, **kwargs):
            return str(backend) if name=='ccache' else real_which(name,**kwargs)
        with patch.object(accel.shutil,'which',side_effect=which):
            env=self.configure({**self.env,'LPM_COMPILER_CACHE':'ccache','CC':compiler+' -DVALUE=7'})
        source=self.root/'test.c'
        source.write_text('int main(void) { return VALUE == 7 ? 0 : 1; }\n')
        for index in range(2):
            output=self.root/f'test-{index}'
            subprocess.run([env['CC'], str(source),'-o',str(output)],env=env,check=True)
            subprocess.run([str(output)],check=True)
        self.assertEqual((Path(env['CCACHE_DIR'])/'calls').read_text().count('cache-called'),2)
        self.assertEqual(env['CCACHE_COMPILERCHECK'],'content')
        self.assertNotIn('CCACHE_SLOPPINESS',env)

    def test_sandbox_visible_paths(self):
        backend=self.fake_cache()
        real_which=shutil.which
        with patch.object(accel.shutil,'which',side_effect=lambda name,**kwargs: str(backend) if name=='ccache' else real_which(name,**kwargs)):
            env=self.configure({**self.env,'LPM_COMPILER_CACHE':'ccache'},sandbox=True)
        self.assertTrue(env['CC'].startswith('/build/accelerators/'))
        self.assertEqual(env['CCACHE_DIR'],'/lpm-compiler-cache')
        self.assertTrue(Path(env['LPM_COMPILER_CACHE_DIR']).is_dir())

    def test_kernel_helper_passes_explicit_compilers(self):
        env=self.configure()
        fake_make=self.root/'make'
        fake_make.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        fake_make.chmod(0o755)
        env.update(PATH=str(self.root)+':'+env['PATH'],CC='cache gcc',HOSTCC='cc')
        result=subprocess.run([env['LPM_KERNEL_MAKE'],'O=out','all'],env=env,text=True,capture_output=True,check=True)
        self.assertIn('CC=cache gcc\n',result.stdout)
        self.assertIn('HOSTCC=cc\n',result.stdout)
        self.assertIn('O=out\n',result.stdout)

    def test_bwrap_binds_only_compiler_cache(self):
        script=self.root/'test.lpmbuild'
        script.write_text('build() { :; }\n')
        cache=self.root/'cache'
        cache.mkdir()
        env={'LPM_COMPILER_CACHE_DIR':str(cache),'LPM_COMPILER_CACHE_ACTIVE':'ccache'}
        with patch.dict(app.CONF,SANDBOX_MODE='bwrap'), patch.object(app.subprocess,'run') as run:
            app.sandboxed_run('build',self.root,env,script,self.root/'pkg',self.root/'build',self.root/'src')
        command=run.call_args.args[0]
        self.assertEqual(command[4:7],['--bind',str(cache),'/lpm-compiler-cache'])
        self.assertIn('--ro-bind',command)

    def test_memory_cgroup_budget(self):
        data={'/proc/meminfo':'MemAvailable: 16777216 kB\n',
              '/sys/fs/cgroup/memory.max':str(4*1024**3),
              '/sys/fs/cgroup/memory.current':str(1024**3)}
        with patch.object(Path,'read_text',lambda path: data[str(path)]):
            self.assertEqual(accel.available_memory_mb(),3072)


if __name__=='__main__':
    unittest.main()
