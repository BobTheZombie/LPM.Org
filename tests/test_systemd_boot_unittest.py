import importlib.machinery
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader('lpm_systemd_boot', str(Path(__file__).resolve().parents[1]/'usr/libexec/lpm/hooks/update-systemd-boot'))
spec = importlib.util.spec_from_loader(loader.name, loader)
hook = importlib.util.module_from_spec(spec)
loader.exec_module(hook)


class SystemdBootTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.esp = self.root/'esp'
        self.esp.mkdir()
        for name, data in [('etc/kernel/cmdline','root=UUID=test rw\n'),
                           ('usr/lib/modules/6.17/vmlinuz','kernel'),
                           ('boot/initrd-6.17.img','initrd')]:
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)
        self.config = {}

    def test_install_copies_payload_and_bls_without_firmware_writes(self):
        with patch.object(hook.subprocess, 'run', return_value=subprocess.CompletedProcess([],0)) as run:
            hook.update(self.root, self.esp, self.config, '/usr/bin/bootctl', True)
        self.assertEqual((self.esp/'lpm/6.17/vmlinuz').read_text(),'kernel')
        self.assertEqual((self.esp/'lpm/6.17/initrd.img').read_text(),'initrd')
        entry = (self.esp/'loader/entries/lpm-6.17.conf').read_text()
        self.assertIn('options root=UUID=test rw',entry)
        self.assertIn('initrd /lpm/6.17/initrd.img',entry)
        self.assertEqual(run.call_args.args[0],['/usr/bin/bootctl','--esp-path='+str(self.esp),'--no-variables','install'])

    def test_missing_initrd_fails_before_boot_write(self):
        (self.root/'boot/initrd-6.17.img').unlink()
        with patch.object(hook.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'Missing initramfs'):
                hook.update(self.root,self.esp,self.config,'bootctl',True)
            run.assert_not_called()
        self.assertEqual(list(self.esp.iterdir()),[])

    def test_update_skips_uninstalled_bootloader(self):
        with patch.object(hook.subprocess,'run',return_value=subprocess.CompletedProcess([],1)) as run:
            hook.update(self.root,self.esp,self.config,'bootctl')
            self.assertEqual(run.call_count,1)
        self.assertEqual(list(self.esp.iterdir()),[])

    def test_existing_loader_config_preserved(self):
        config = self.esp/'loader/loader.conf'
        config.parent.mkdir()
        config.write_text('timeout 9\n')
        with patch.object(hook.subprocess,'run',return_value=subprocess.CompletedProcess([],0)):
            hook.update(self.root,self.esp,self.config,'bootctl',True)
        self.assertEqual(config.read_text(),'timeout 9\n')

    def test_microcode_precedes_initramfs(self):
        (self.root/'boot/amd.img').write_text('microcode')
        entries=hook.prepare_entries(self.root,self.esp,{'microcode':'/boot/amd.img'})
        text=entries[0][2]
        self.assertLess(text.index('microcode-0.img'),text.index('initrd.img'))

    def test_unmanaged_entry_not_overwritten(self):
        entry=self.esp/'loader/entries/lpm-6.17.conf'
        entry.parent.mkdir(parents=True)
        entry.write_text('custom entry')
        with self.assertRaisesRegex(RuntimeError,'unmanaged'):
            hook.prepare_entries(self.root,self.esp,self.config)

    def test_escaping_symlink_rejected(self):
        outside=self.root/'outside'
        outside.mkdir()
        (self.esp/'lpm').symlink_to(outside)
        with self.assertRaisesRegex(RuntimeError,'escapes'):
            hook.prepare_entries(self.root,self.esp,self.config)

    def test_placeholder_cmdline_rejected(self):
        (self.root/'etc/kernel/cmdline').write_text('root=UUID=CHANGE-ME rw')
        with self.assertRaisesRegex(RuntimeError,'configured kernel command'):
            hook.prepare_entries(self.root,self.esp,self.config)

    def test_mounts_unmounted_esp_using_fstab(self):
        (self.root/'etc/fstab').write_text(
            f'UUID=1234 {self.esp} vfat defaults 0 2\n'
        )
        with patch.object(hook.os.path, 'ismount', side_effect=[False, True]), \
             patch.object(hook.shutil, 'which', return_value='/usr/bin/mount'), \
             patch.object(hook.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            hook.ensure_esp_mounted(self.root, self.esp)
        run.assert_called_once_with(['/usr/bin/mount', str(self.esp)], check=True)

    def test_unmounted_esp_without_fstab_entry_fails(self):
        (self.root/'etc/fstab').write_text('UUID=1234 /other vfat defaults 0 2\n')
        with patch.object(hook.os.path, 'ismount', return_value=False), \
             patch.object(hook.subprocess, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'no /etc/fstab entry'):
                hook.ensure_esp_mounted(self.root, self.esp)
        run.assert_not_called()

    def test_mount_command_must_result_in_mounted_esp(self):
        (self.root/'etc/fstab').write_text(
            f'UUID=1234 {self.esp} vfat defaults 0 2\n'
        )
        with patch.object(hook.os.path, 'ismount', return_value=False), \
             patch.object(hook.shutil, 'which', return_value='/usr/bin/mount'), \
             patch.object(hook.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)):
            with self.assertRaisesRegex(RuntimeError, 'remains unmounted'):
                hook.ensure_esp_mounted(self.root, self.esp)

    def test_alternate_root_skips(self):
        with patch.dict(hook.os.environ,LPM_ROOT=str(self.root)),patch.object(hook.subprocess,'run') as run:
            self.assertEqual(hook.main([]),0)
            run.assert_not_called()


if __name__=='__main__':
    unittest.main()
