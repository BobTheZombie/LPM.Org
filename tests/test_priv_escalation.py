import sys

import pytest

from src.lpm import priv
from src.lpm.cli import as_root


@pytest.fixture(autouse=True)
def reset_argv(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["lpm", "install"])


def test_require_root_allows_privileged(monkeypatch):
    monkeypatch.setattr(priv.os, "geteuid", lambda: 0)
    priv.require_root("install packages")


def test_require_root_raises_without_privileges(monkeypatch):
    monkeypatch.setattr(priv.os, "geteuid", lambda: 1000)
    with pytest.raises(priv.RootPrivilegesRequired) as exc:
        priv.require_root("install packages")
    assert "install packages" in str(exc.value)
    assert as_root.AS_ROOT_FLAG in str(exc.value)


def test_format_command_for_hint_uses_current_argv(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["lpm", "install", "pkg"])
    hint = priv.format_command_for_hint()
    assert "lpm" in hint
    assert "pkg" in hint


def test_ensure_root_wrapper(monkeypatch):
    monkeypatch.setattr(priv.os, "geteuid", lambda: 1000)
    with pytest.raises(priv.RootPrivilegesRequired):
        priv.ensure_root_or_escalate("install packages")

