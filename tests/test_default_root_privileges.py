from __future__ import annotations

from pathlib import Path

import pytest

from lpm import app as lpm
from lpm.fs_ops import prepare_directory


def _raise_die(message: str, _code: int = 2):
    raise RuntimeError(message)


def test_removepkg_default_root_requires_privileges(monkeypatch):
    monkeypatch.setattr(lpm, "_is_default_root", lambda root: True)
    monkeypatch.setattr(lpm.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(lpm, "privileges_enabled", lambda: False)
    monkeypatch.setattr(lpm, "die", _raise_die)

    with pytest.raises(RuntimeError, match="removepkg requires root privileges when removing from the default root"):
        lpm.removepkg("demo", root=Path(lpm.DEFAULT_ROOT))


def test_removepkg_non_default_root_skips_privilege_requirement(monkeypatch, tmp_path):
    monkeypatch.setattr(lpm, "_is_default_root", lambda root: False)
    monkeypatch.setattr(lpm.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(lpm, "privileges_enabled", lambda: False)
    monkeypatch.setattr(lpm, "die", _raise_die)

    class _Conn:
        def execute(self, *_args, **_kwargs):
            class _Cursor:
                def fetchone(self):
                    return None

            return _Cursor()

    monkeypatch.setattr(lpm, "db", lambda: _Conn())

    # Should not fail privilege check for non-default roots.
    lpm.removepkg("demo", root=tmp_path)


def test_installpkg_default_root_uses_privileged_section_when_available(monkeypatch, tmp_path):
    calls = []

    @lpm.contextlib.contextmanager
    def fake_privileged_section():
        calls.append("enter")
        yield
        calls.append("exit")

    pkg = tmp_path / f"demo{lpm.EXT}"
    pkg.write_bytes(b"\x28\xb5\x2f\xfdpayload")
    meta = lpm.PkgMeta(name="demo", version="1.0")

    monkeypatch.setattr(lpm, "_is_default_root", lambda root: True)
    monkeypatch.setattr(lpm.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(lpm, "privileges_enabled", lambda: True)
    monkeypatch.setattr(lpm, "privileged_section", fake_privileged_section)
    monkeypatch.setattr(lpm, "load_protected", lambda: ["demo"])
    monkeypatch.setattr(lpm, "read_package_meta", lambda _pkg: (meta, []))

    assert lpm.installpkg(pkg, root=Path(lpm.DEFAULT_ROOT), verify=False) is meta
    assert calls == ["enter", "exit"]


def test_installpkg_default_root_dry_run_does_not_use_privileged_section(monkeypatch, tmp_path):
    calls = []

    @lpm.contextlib.contextmanager
    def fake_privileged_section():
        calls.append("enter")
        yield
        calls.append("exit")

    pkg = tmp_path / f"demo{lpm.EXT}"
    pkg.write_bytes(b"\x28\xb5\x2f\xfdpayload")
    meta = lpm.PkgMeta(name="demo", version="1.0")

    monkeypatch.setattr(lpm, "_is_default_root", lambda root: True)
    monkeypatch.setattr(lpm.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(lpm, "privileges_enabled", lambda: True)
    monkeypatch.setattr(lpm, "privileged_section", fake_privileged_section)
    monkeypatch.setattr(lpm, "load_protected", lambda: [])
    monkeypatch.setattr(lpm, "read_package_meta", lambda _pkg: (meta, []))

    assert lpm.installpkg(pkg, root=Path(lpm.DEFAULT_ROOT), dry_run=True, verify=False) is meta
    assert calls == []


def test_removepkg_default_root_uses_privileged_section_when_available(monkeypatch):
    calls = []

    @lpm.contextlib.contextmanager
    def fake_privileged_section():
        calls.append("enter")
        yield
        calls.append("exit")

    monkeypatch.setattr(lpm, "_is_default_root", lambda root: True)
    monkeypatch.setattr(lpm.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(lpm, "privileges_enabled", lambda: True)
    monkeypatch.setattr(lpm, "privileged_section", fake_privileged_section)
    monkeypatch.setattr(lpm, "load_protected", lambda: ["demo"])

    lpm.removepkg("demo", root=Path(lpm.DEFAULT_ROOT))
    assert calls == ["enter", "exit"]


def test_removepkg_default_root_dry_run_does_not_use_privileged_section(monkeypatch):
    calls = []

    @lpm.contextlib.contextmanager
    def fake_privileged_section():
        calls.append("enter")
        yield
        calls.append("exit")

    monkeypatch.setattr(lpm, "_is_default_root", lambda root: True)
    monkeypatch.setattr(lpm.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(lpm, "privileges_enabled", lambda: True)
    monkeypatch.setattr(lpm, "privileged_section", fake_privileged_section)
    monkeypatch.setattr(lpm, "load_protected", lambda: ["demo"])

    lpm.removepkg("demo", root=Path(lpm.DEFAULT_ROOT), dry_run=True)
    assert calls == []


def test_prepare_directory_permission_fallback(tmp_path, monkeypatch):
    target = tmp_path / "blocked"
    fallback = tmp_path / "fallback-dir"

    def _raise_mkdir(*_args, **_kwargs):
        raise PermissionError("blocked")

    monkeypatch.setattr(Path, "mkdir", _raise_mkdir)
    monkeypatch.setattr(lpm.tempfile, "mkdtemp", lambda prefix: str(fallback))
    result = prepare_directory(target, privileged=True, reset=True, fallback_prefix="lpm-test-")
    assert result == fallback
