"""Safety and idempotence tests for LPM's opt-in user-groups hook."""
from __future__ import annotations

import os
import runpy
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "usr/libexec/lpm/hooks/user-groups"


def _target(tmp_path: Path, entries: str) -> Path:
    root = tmp_path / "root"
    etc = root / "etc"
    config_dir = etc / "lpm/user-groups.d"
    config_dir.mkdir(parents=True)
    (etc / "passwd").write_text(
        "root:x:0:0:root:/root:/bin/sh\n"
        "alice:x:1000:1000:Alice:/home/alice:/bin/bash\n"
        "service:x:100:100:Daemon:/var/lib/service:/usr/bin/nologin\n",
        encoding="utf-8",
    )
    (etc / "group").write_text(
        "root:x:0:\nusers:x:1000:alice\nwheel:x:10:\nvideo:x:39:\n",
        encoding="utf-8",
    )
    (config_dir / "desktop.conf").write_text(entries, encoding="utf-8")
    return root


def _load_main(monkeypatch, root: Path, calls: list[list[str]]):
    main = runpy.run_path(str(SCRIPT), run_name="lpm_test")["main"]
    monkeypatch.setenv("LPM_ROOT", str(root))
    real_stat = Path.stat
    config_dir = root / "etc/lpm/user-groups.d"

    def fake_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if Path(path).parent == config_dir:
            return SimpleNamespace(st_uid=0, st_mode=result.st_mode)
        return result

    monkeypatch.setattr(Path, "stat", fake_stat)
    monkeypatch.setattr(main.__globals__["shutil"], "which", lambda _: "/usr/sbin/usermod")

    def fake_run(cmd, *, check):
        calls.append(cmd)
        assert check is False
        assert cmd[1:3] == ["--root", str(root)]
        assert cmd[3:5] == ["--append", "--groups"]
        group, username = cmd[-2:]
        lines = (root / "etc/group").read_text(encoding="utf-8").splitlines()
        lines = [
            line + username if line.startswith(group + ":") and line.endswith(":") else line
            for line in lines
        ]
        (root / "etc/group").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(main.__globals__["subprocess"], "run", fake_run)
    return main


def test_enroll_existing_user_only_once(tmp_path, monkeypatch):
    root = _target(tmp_path, "alice wheel video\n")
    calls = []
    main = _load_main(monkeypatch, root, calls)
    assert main() == 0
    assert len(calls) == 2
    assert main() == 0
    assert len(calls) == 2


def test_never_auto_create_accounts_or_groups(tmp_path, monkeypatch):
    root = _target(tmp_path, "missing wheel\nalice missing_group\nservice wheel\n")
    calls = []
    main = _load_main(monkeypatch, root, calls)
    assert main() == 1  # Refuses privileged changes to system accounts.
    assert calls == []


def test_no_admin_rules_is_no_op(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("LPM_ROOT", str(root))
    main = runpy.run_path(str(SCRIPT), run_name="lpm_test")["main"]
    assert main() == 0


def test_rejects_world_writable_rules(tmp_path, monkeypatch):
    root = _target(tmp_path, "alice wheel\n")
    config = root / "etc/lpm/user-groups.d/desktop.conf"
    config.chmod(0o666)
    calls = []
    main = _load_main(monkeypatch, root, calls)
    assert main() == 1
    assert calls == []
