from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import List, Tuple


def _reload_privileges(monkeypatch, *, real_ids, effective_ids, env_ids=None):
    real_uid, real_gid = real_ids
    eff_uid, eff_gid = effective_ids
    env_ids = env_ids or {}

    for key in ("SUDO_UID", "SUDO_GID", "PKEXEC_UID", "PKEXEC_GID"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env_ids.items():
        monkeypatch.setenv(key, str(value))

    project_root = Path(__file__).resolve().parent.parent
    src_path = project_root / "src"
    added_src = False
    if str(src_path) not in sys.path:
        sys.path.insert(0, str(src_path))
        added_src = True

    monkeypatch.setattr(os, "getuid", lambda: real_uid)
    monkeypatch.setattr(os, "getgid", lambda: real_gid)
    monkeypatch.setattr(os, "geteuid", lambda: eff_uid)
    monkeypatch.setattr(os, "getegid", lambda: eff_gid)

    calls: List[Tuple[str, Tuple[int, int, int]]] = []

    def record(name):
        def _inner(*args):
            calls.append((name, tuple(int(a) for a in args)))
        return _inner

    monkeypatch.setattr(os, "setresuid", record("setresuid"), raising=False)
    monkeypatch.setattr(os, "setresgid", record("setresgid"), raising=False)

    sys.modules.pop("src.lpm.privileges", None)
    module = importlib.import_module("src.lpm.privileges")

    def cleanup() -> None:
        sys.modules.pop("src.lpm.privileges", None)
        if added_src:
            try:
                sys.path.remove(str(src_path))
            except ValueError:
                pass

    return module, calls, cleanup


def test_privileged_section_drops_and_restores(monkeypatch):
    module, calls, cleanup = _reload_privileges(
        monkeypatch,
        real_ids=(1000, 1000),
        effective_ids=(0, 0),
    )
    try:
        # Drop occurred during module import: gid then uid.
        assert calls[:2] == [
            ("setresgid", (1000, 1000, 0)),
            ("setresuid", (1000, 1000, 0)),
        ]

        calls.clear()

        with module.privileged_section():
            pass

        # Escalate for the duration of the context and drop afterwards.
        assert calls == [
            ("setresuid", (1000, 0, 0)),
            ("setresgid", (1000, 0, 0)),
            ("setresgid", (1000, 1000, 0)),
            ("setresuid", (1000, 1000, 0)),
        ]
    finally:
        cleanup()


def test_privileged_section_disabled_without_elevation(monkeypatch):
    module, calls, cleanup = _reload_privileges(
        monkeypatch,
        real_ids=(1000, 1000),
        effective_ids=(1000, 1000),
    )
    try:
        assert not module._MANAGER.enabled

        calls.clear()

        with module.privileged_section():
            pass

        assert calls == []
    finally:
        cleanup()
