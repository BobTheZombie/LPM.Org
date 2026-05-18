from __future__ import annotations

from pathlib import Path

import pytest

from lpm import app as lpm


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
