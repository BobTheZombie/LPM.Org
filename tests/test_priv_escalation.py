import builtins

import pytest

from src.lpm import priv


@pytest.fixture(autouse=True)
def reset_priv_state(monkeypatch):
    monkeypatch.setattr(priv, "_AUTO_ESCALATION_DISABLED", False)
    monkeypatch.setattr(priv, "_PROMPT_CONTEXT", "default")
    monkeypatch.setattr(priv.sys, "argv", ["lpm", "install"])
    yield


def test_ensure_root_returns_when_root(monkeypatch):
    monkeypatch.setattr(priv.os, "geteuid", lambda: 0)
    priv.ensure_root_or_escalate("install packages")


def test_ensure_root_tty_uses_sudo(monkeypatch):
    monkeypatch.setattr(priv.os, "geteuid", lambda: 1000)
    fake_stdin = type("_S", (), {"isatty": lambda self=None: True})()
    monkeypatch.setattr(priv.sys, "stdin", fake_stdin)
    monkeypatch.setattr(priv.shutil, "which", lambda name: "/usr/bin/sudo" if name == "sudo" else None)

    calls = []

    def fake_execvp(prog, argv):
        calls.append((prog, list(argv)))
        raise RuntimeError("execvp invoked")

    monkeypatch.setattr(priv.os, "execvp", fake_execvp)
    def fake_execvpe(*_args, **_kwargs):
        raise AssertionError("pkexec should not be used")

    monkeypatch.setattr(priv.os, "execvpe", fake_execvpe)
    monkeypatch.setattr(builtins, "input", lambda prompt="": "")

    with pytest.raises(RuntimeError):
        priv.ensure_root_or_escalate("install packages")

    assert calls
    prog, argv = calls[0]
    assert prog == "sudo"
    assert argv[0] == "sudo"
    assert argv[1] == "-E"
    assert argv[2:] == ["lpm", "install"]


def test_ensure_root_disabled_exits(monkeypatch):
    monkeypatch.setattr(priv.os, "geteuid", lambda: 1000)
    priv.set_escalation_disabled(True)
    with pytest.raises(SystemExit) as exc:
        priv.ensure_root_or_escalate("install packages")
    assert exc.value.code == 77
