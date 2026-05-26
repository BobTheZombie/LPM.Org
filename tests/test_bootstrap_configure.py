from pathlib import Path

import pytest

import lpm.bootstrap as bootstrap


def test_generate_fstab_uefi_with_uuid_fallback(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bootstrap, "_blkid_uuid", lambda dev: "ROOT-UUID" if dev == "/dev/root" else None)
    path = bootstrap.generate_fstab(
        tmp_path,
        "uefi",
        Path("/boot/efi"),
        {"root": "/dev/root", "boot": "/dev/boot", "efi": "/dev/efi"},
    )
    text = path.read_text(encoding="utf-8")
    assert "UUID=ROOT-UUID / ext4" in text
    assert "/dev/boot /boot ext4" in text
    assert "/dev/efi /boot/efi vfat" in text


def test_generate_fstab_bios_without_efi(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(bootstrap, "_blkid_uuid", lambda dev: None)
    text = bootstrap.generate_fstab(tmp_path, "bios", None, {"root": "/dev/sda2"}).read_text(encoding="utf-8")
    assert "/boot/efi" not in text


def test_safe_target_rejects_dangerous() -> None:
    with pytest.raises(ValueError):
        bootstrap._safe_target(Path("/"))


def test_generate_chroot_command() -> None:
    cmd = bootstrap.generate_chroot_command(Path("/mnt"), ["echo", "ok"])
    assert cmd == ["chroot", "/mnt", "echo", "ok"]


def test_state_tracking_dry_run(tmp_path: Path) -> None:
    cfg = bootstrap.BootstrapConfig(target=tmp_path, dry_run=True)
    bootstrap.save_state(cfg, {"completed_stages": ["validate"]})
    assert not cfg.state_path.exists()


def test_grub_install_requires_boot_device_for_bios() -> None:
    with pytest.raises(ValueError):
        bootstrap.grub_install_command("bios", None, None)


def test_generate_lpm_root_install_command() -> None:
    cmd = bootstrap.generate_lpm_root_install_command(Path('/mnt'), ['basesystem', 'linux'])
    assert cmd == ['lpm', '--root', '/mnt', 'install', 'basesystem', 'linux']


def test_generate_lpm_root_install_command_requires_packages() -> None:
    with pytest.raises(ValueError):
        bootstrap.generate_lpm_root_install_command(Path('/mnt'), [])


def test_install_base_uses_lpm_not_dnf(monkeypatch, tmp_path: Path) -> None:
    cfg = bootstrap.BootstrapConfig(target=tmp_path, bootloader='grub', kernel='linux')
    calls: list[list[str]] = []

    def fake_run(cmd, check=True, **kwargs):
        calls.append(list(cmd))
        class Result:
            returncode = 0
        return Result()

    monkeypatch.setattr(bootstrap.subprocess, 'run', fake_run)
    bootstrap._run_stage(cfg, bootstrap.Stage.INSTALL_BASE, bootstrap.ChrootMountState())
    assert calls
    assert calls[0][0] == 'lpm'
    assert 'dnf' not in calls[0]
