from __future__ import annotations

from pathlib import Path

from lpm import chroot


def test_is_mounted_detection(tmp_path: Path) -> None:
    target = tmp_path / "root/proc"
    assert chroot._is_mounted(target, {str(target)})
    assert not chroot._is_mounted(target, set())


def test_mount_chroot_api_avoids_double_mount(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "root"
    commands: list[list[str]] = []

    monkeypatch.setattr(chroot, "_mount_table", lambda: {str(root / "proc")})
    monkeypatch.setattr(chroot, "_run", lambda cmd: commands.append(list(cmd)))

    state = chroot.mount_chroot_api(root)

    assert str(root / "proc") not in " ".join(" ".join(c) for c in commands)
    assert state.mounted == ["proc", "sys", "dev", "dev/pts", "run"]
    assert len(commands) == 4


def test_umount_chroot_api_reverse_order(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "root"
    called: list[str] = []
    mounted = {
        str(root / "proc"),
        str(root / "sys"),
        str(root / "dev"),
        str(root / "dev/pts"),
        str(root / "run"),
    }

    monkeypatch.setattr(chroot, "_mount_table", lambda: set(mounted))

    def fake_run(cmd: list[str]) -> None:
        called.append(cmd[-1])

    monkeypatch.setattr(chroot, "_run", fake_run)

    state = chroot.ChrootMountState(mounted=["proc", "sys", "dev", "dev/pts", "run"])
    chroot.umount_chroot_api(root, state)

    assert called == [
        str(root / "run"),
        str(root / "dev/pts"),
        str(root / "dev"),
        str(root / "sys"),
        str(root / "proc"),
    ]
    assert state.mounted == []
