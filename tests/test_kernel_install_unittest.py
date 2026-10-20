import importlib.machinery
import importlib.util
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
from unittest.mock import patch

HOOK_DIR = Path(__file__).resolve().parents[1] / 'usr/libexec/lpm/hooks'
sys.path.insert(0, str(HOOK_DIR))
loader = importlib.machinery.SourceFileLoader('lpm_kernel_install', str(HOOK_DIR / 'kernel-install'))
spec = importlib.util.spec_from_loader(loader.name, loader)
hook = importlib.util.module_from_spec(spec)
loader.exec_module(hook)


class KernelInstallTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'etc').mkdir()
        (self.root / 'usr/lib').mkdir(parents=True)

    def test_systemd_x86_64_includes_loader(self):
        (self.root / 'etc/mkinitcpio.conf').write_text(
            'HOOKS=(\n  base\n  systemd\n  # comment\n  block filesystems\n)\n'
        )
        (self.root / 'usr/lib/ld-linux-x86-64.so.2').write_bytes(b'loader')
        with patch.object(hook.platform, 'machine', return_value='x86_64'):
            self.assertEqual(
                hook._mkinitcpio_extra_args(self.root),
                ['--include', '/usr/lib/ld-linux-x86-64.so.2']
            )

    def test_other_initramfs_profile_does_not_require_systemd_loader(self):
        (self.root / 'etc/mkinitcpio.conf').write_text('HOOKS=(base udev block filesystems)\n')
        with patch.object(hook.platform, 'machine', return_value='x86_64'):
            self.assertEqual(hook._mkinitcpio_extra_args(self.root), [])

    def test_other_architecture_does_not_include_x86_loader(self):
        (self.root / 'etc/mkinitcpio.conf').write_text('HOOKS=(systemd block filesystems)\n')
        with patch.object(hook.platform, 'machine', return_value='aarch64'):
            self.assertEqual(hook._mkinitcpio_extra_args(self.root), [])

    def test_systemd_missing_loader_fails(self):
        (self.root / 'etc/mkinitcpio.conf').write_text('HOOKS=(systemd block filesystems)\n')
        with patch.object(hook.platform, 'machine', return_value='x86_64'):
            with self.assertRaisesRegex(RuntimeError, 'requires ELF interpreter'):
                hook._mkinitcpio_extra_args(self.root)


    def test_missing_loader_in_successful_image_fails_validation(self):
        (self.root / 'etc/mkinitcpio.conf').write_text('HOOKS=(systemd block filesystems)\n')
        (self.root / 'usr/lib/ld-linux-x86-64.so.2').write_bytes(b'loader')
        listing = subprocess.CompletedProcess([], 0, stdout='init\nusr/lib/systemd/systemd\n')
        with patch.object(hook.platform, 'machine', return_value='x86_64'), \
             patch.object(hook, '_run_command'), \
             patch.object(hook.subprocess, 'run', return_value=listing):
            with self.assertRaisesRegex(RuntimeError, 'lacks required systemd ELF interpreter'):
                hook._run_mkinitcpio(self.root, '7.2.11-lpm2')



if __name__ == '__main__':
    unittest.main()
