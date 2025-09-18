import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lpm


@pytest.fixture
def root(tmp_path):
    test_root = tmp_path / "root"
    test_root.mkdir()
    return test_root


def _create_service(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[Unit]\nDescription=Test service\n", encoding="utf-8")


def test_handle_service_files_systemd_multiple_dirs(root, monkeypatch, capsys):
    service_usr = root / "usr/lib/systemd/system/foo.service"
    service_lib_foo = root / "lib/systemd/system/foo.service"
    service_lib_bar = root / "lib/systemd/system/bar.service"

    for svc in (service_usr, service_lib_foo, service_lib_bar):
        _create_service(svc)

    monkeypatch.setitem(lpm.CONF, "INIT_POLICY", "auto")
    monkeypatch.setattr(lpm, "detect_init_system", lambda: "systemd")

    calls = []

    def fake_run(cmd, check=False, **kwargs):
        calls.append(cmd)

    monkeypatch.setattr(lpm.subprocess, "run", fake_run)

    lpm.handle_service_files("dummy", root)

    err = capsys.readouterr().err

    assert err.count("foo.service") == 2
    assert "usr/lib/systemd/system" in err
    assert "lib/systemd/system" in err
    assert err.count("bar.service") == 1

    assert calls == [
        ["systemctl", "enable", "--now", "foo.service"],
        ["systemctl", "enable", "--now", "bar.service"],
    ]


def test_remove_service_files_systemd_multiple_dirs(root, monkeypatch, capsys):
    service_usr = root / "usr/lib/systemd/system/foo.service"
    service_lib_foo = root / "lib/systemd/system/foo.service"
    service_lib_bar = root / "lib/systemd/system/bar.service"

    for svc in (service_usr, service_lib_foo, service_lib_bar):
        _create_service(svc)

    monkeypatch.setitem(lpm.CONF, "INIT_POLICY", "auto")
    monkeypatch.setattr(lpm, "detect_init_system", lambda: "systemd")

    calls = []

    def fake_run(cmd, check=False, **kwargs):
        calls.append(cmd)

    monkeypatch.setattr(lpm.subprocess, "run", fake_run)

    lpm.remove_service_files("dummy", root)

    err = capsys.readouterr().err

    assert err.count("foo.service") == 2
    assert "usr/lib/systemd/system" in err
    assert "lib/systemd/system" in err
    assert err.count("bar.service") == 1

    assert calls == [
        ["systemctl", "disable", "--now", "foo.service"],
        ["systemctl", "disable", "--now", "bar.service"],
    ]
