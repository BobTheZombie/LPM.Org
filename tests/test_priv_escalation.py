import builtins
import importlib
import os
from pathlib import Path

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
    monkeypatch.setattr(
        priv.shutil,
        "which",
        lambda name: "/usr/bin/sudo" if name == "sudo" else None,
    )

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


def test_ensure_root_onefile_uses_module(monkeypatch):
    monkeypatch.setattr(priv.os, "geteuid", lambda: 1000)
    fake_stdin = type("_S", (), {"isatty": lambda self=None: True})()
    monkeypatch.setattr(priv.sys, "stdin", fake_stdin)

    def fake_which(name):
        if name == "sudo":
            return "/usr/bin/sudo"
        if name == "python3":
            return "/usr/bin/python3"
        return None

    monkeypatch.setattr(priv.shutil, "which", fake_which)
    monkeypatch.setattr(priv.os, "access", lambda path, mode: True)

    calls = []

    def fake_execvp(prog, argv):
        calls.append((prog, list(argv)))
        raise RuntimeError("execvp invoked")

    monkeypatch.setattr(priv.os, "execvp", fake_execvp)

    def fake_execvpe(*_args, **_kwargs):
        raise AssertionError("pkexec should not be used")

    monkeypatch.setattr(priv.os, "execvpe", fake_execvpe)
    monkeypatch.setattr(priv.sys, "argv", ["/tmp/onefile_123/python3", "install", "pkg"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": "y")

    with pytest.raises(RuntimeError):
        priv.ensure_root_or_escalate("install packages")

    assert calls
    prog, argv = calls[0]
    assert prog == "sudo"
    assert argv[:2] == ["sudo", "-E"]
    assert argv[2] == "/usr/bin/python3"
    assert argv[3] == "-c"
    assert argv[4] == priv._FALLBACK_SCRIPT
    assert argv[5:] == ["install", "pkg"]


def test_ensure_root_onefile_via_executable(monkeypatch):
    monkeypatch.setattr(priv.os, "geteuid", lambda: 1000)
    fake_stdin = type("_S", (), {"isatty": lambda self=None: True})()
    monkeypatch.setattr(priv.sys, "stdin", fake_stdin)

    def fake_which(name):
        if name == "sudo":
            return "/usr/bin/sudo"
        if name == "python3":
            return "/usr/bin/python3"
        return None

    monkeypatch.setattr(priv.shutil, "which", fake_which)
    monkeypatch.setattr(priv.os, "access", lambda path, mode: True)

    calls = []

    def fake_execvp(prog, argv):
        calls.append((prog, list(argv)))
        raise RuntimeError("execvp invoked")

    monkeypatch.setattr(priv.os, "execvp", fake_execvp)

    def fake_execvpe(*_args, **_kwargs):
        raise AssertionError("pkexec should not be used")

    monkeypatch.setattr(priv.os, "execvpe", fake_execvpe)
    monkeypatch.setattr(priv.sys, "argv", ["lpm", "install", "pkg"])
    monkeypatch.setattr(priv.sys, "executable", "/tmp/onefile_456/python3")
    monkeypatch.setattr(builtins, "input", lambda prompt="": "y")

    with pytest.raises(RuntimeError):
        priv.ensure_root_or_escalate("install packages")

    assert calls
    prog, argv = calls[0]
    assert prog == "sudo"
    assert argv[:2] == ["sudo", "-E"]
    assert argv[2] == "/usr/bin/python3"
    assert argv[3] == "-c"
    assert argv[4] == priv._FALLBACK_SCRIPT
    assert argv[5:] == ["install", "pkg"]


def test_ensure_root_disabled_exits(monkeypatch):
    monkeypatch.setattr(priv.os, "geteuid", lambda: 1000)
    priv.set_escalation_disabled(True)
    with pytest.raises(SystemExit) as exc:
        priv.ensure_root_or_escalate("install packages")
    assert exc.value.code == 77


def test_onefile_fallback_sets_pythonpath(monkeypatch):
    monkeypatch.setattr(priv.os, "geteuid", lambda: 1000)
    fake_stdin = type("_S", (), {"isatty": lambda self=None: True})()
    monkeypatch.setattr(priv.sys, "stdin", fake_stdin)

    def fake_which(name):
        if name == "sudo":
            return "/usr/bin/sudo"
        if name == "python3":
            return "/usr/bin/python3"
        return None

    monkeypatch.setattr(priv.shutil, "which", fake_which)
    monkeypatch.setattr(priv.os, "access", lambda path, mode: True)
    monkeypatch.setattr(priv.sys, "argv", ["/tmp/onefile_demo/python3", "install", "pkg"])
    monkeypatch.setattr(builtins, "input", lambda prompt="": "y")
    monkeypatch.delenv("PYTHONPATH", raising=False)

    captured = {}

    def fake_execvp(_prog, _argv):
        captured["pythonpath"] = os.environ.get("PYTHONPATH")
        raise RuntimeError("execvp invoked")

    monkeypatch.setattr(priv.os, "execvp", fake_execvp)

    def fake_execvpe(*_args, **_kwargs):
        raise AssertionError("pkexec should not be used")

    monkeypatch.setattr(priv.os, "execvpe", fake_execvpe)

    with pytest.raises(RuntimeError):
        priv.ensure_root_or_escalate("install packages")

    pythonpath = captured.get("pythonpath")
    assert pythonpath

    module = importlib.import_module("lpm")
    module_path = Path(module.__file__).resolve()
    package_dir = module_path.parent
    if package_dir.name == "lpm":
        expected_root = package_dir.parent
    else:
        expected_root = package_dir

    assert str(expected_root) in pythonpath.split(os.pathsep)
