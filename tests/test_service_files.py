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
    timer_lib = root / "lib/systemd/system/baz.timer"

    for svc in (service_usr, service_lib_foo, service_lib_bar, timer_lib):
        _create_service(svc)

    monkeypatch.setitem(lpm.CONF, "INIT_POLICY", "auto")
    monkeypatch.setattr(lpm, "detect_init_system", lambda: "systemd")
    monkeypatch.setattr(lpm, "_is_default_root", lambda root: True)

    calls = []

    def fake_run(cmd, check=False, **kwargs):
        calls.append(cmd)

    monkeypatch.setattr(lpm.subprocess, "run", fake_run)

    manifest = [
        {"path": "/usr/lib/systemd/system/foo.service"},
        {"path": "/lib/systemd/system/foo.service"},
        {"path": "/lib/systemd/system/bar.service"},
        {"path": "/lib/systemd/system/baz.timer"},
    ]

    lpm.handle_service_files("dummy", root, manifest)

    err = capsys.readouterr().err

    assert (
        "[ Systemd Service Handler ] detected units foo.service, bar.service, baz.timer; activation will follow automatically."
        in err
    )
    assert (
        "[ Systemd Service Handler ] activating detected units via systemctl enable --now"
        in err
    )
    assert err.count("[ Systemd Service Handler ] detected units") == 1

    assert calls == [
        ["systemctl", "enable", "--now", "foo.service"],
        ["systemctl", "enable", "--now", "bar.service"],
        ["systemctl", "enable", "--now", "baz.timer"],
    ]


def test_remove_service_files_systemd_multiple_dirs(root, monkeypatch, capsys):
    service_usr = root / "usr/lib/systemd/system/foo.service"
    service_lib_foo = root / "lib/systemd/system/foo.service"
    service_lib_bar = root / "lib/systemd/system/bar.service"
    timer_lib = root / "lib/systemd/system/baz.timer"

    for svc in (service_usr, service_lib_foo, service_lib_bar, timer_lib):
        _create_service(svc)

    monkeypatch.setitem(lpm.CONF, "INIT_POLICY", "auto")
    monkeypatch.setattr(lpm, "detect_init_system", lambda: "systemd")
    monkeypatch.setattr(lpm, "_is_default_root", lambda root: True)

    calls = []

    def fake_run(cmd, check=False, **kwargs):
        calls.append(cmd)

    monkeypatch.setattr(lpm.subprocess, "run", fake_run)

    manifest = [
        {"path": "/usr/lib/systemd/system/foo.service"},
        {"path": "/lib/systemd/system/foo.service"},
        {"path": "/lib/systemd/system/bar.service"},
        {"path": "/lib/systemd/system/baz.timer"},
    ]

    lpm.remove_service_files("dummy", root, manifest)

    err = capsys.readouterr().err

    assert err.count("foo.service") == 2
    assert "usr/lib/systemd/system" in err
    assert "lib/systemd/system" in err
    assert err.count("bar.service") == 1
    assert err.count("baz.timer") == 1

    assert calls == [
        ["systemctl", "disable", "--now", "foo.service"],
        ["systemctl", "disable", "--now", "bar.service"],
        ["systemctl", "disable", "--now", "baz.timer"],
    ]


def test_handle_service_files_systemd_non_default_root_skips_systemctl(root, monkeypatch, capsys):
    service = root / "lib/systemd/system/foo.service"
    _create_service(service)

    monkeypatch.setitem(lpm.CONF, "INIT_POLICY", "auto")
    monkeypatch.setattr(lpm, "detect_init_system", lambda: "systemd")

    calls = []

    def fake_run(cmd, check=False, **kwargs):
        calls.append(cmd)

    monkeypatch.setattr(lpm.subprocess, "run", fake_run)

    manifest = [
        {"path": "/lib/systemd/system/foo.service"},
    ]

    lpm.handle_service_files("dummy", root, manifest)

    err = capsys.readouterr().err

    assert (
        "[ Systemd Service Handler ] detected units foo.service; activation will follow on the target system."
        in err
    )
    assert "[ Systemd Service Handler ] activating detected units" not in err
    assert "Skipping systemctl enable" in err
    assert calls == []


def test_remove_service_files_systemd_non_default_root_skips_systemctl(root, monkeypatch, capsys):
    service = root / "lib/systemd/system/foo.service"
    _create_service(service)

    monkeypatch.setitem(lpm.CONF, "INIT_POLICY", "auto")
    monkeypatch.setattr(lpm, "detect_init_system", lambda: "systemd")

    calls = []

    def fake_run(cmd, check=False, **kwargs):
        calls.append(cmd)

    monkeypatch.setattr(lpm.subprocess, "run", fake_run)

    manifest = [
        {"path": "/lib/systemd/system/foo.service"},
    ]

    lpm.remove_service_files("dummy", root, manifest)

    err = capsys.readouterr().err

    assert "foo.service" in err
    assert "Skipping systemctl disable" in err
    assert calls == []


def test_handle_service_files_ignores_units_not_in_manifest(root, monkeypatch, capsys):
    tracked = root / "lib/systemd/system/foo.service"
    untracked = root / "lib/systemd/system/other.service"
    _create_service(tracked)
    _create_service(untracked)

    manifest = [
        {"path": "/lib/systemd/system/foo.service"},
    ]

    monkeypatch.setitem(lpm.CONF, "INIT_POLICY", "auto")
    monkeypatch.setattr(lpm, "detect_init_system", lambda: "systemd")
    monkeypatch.setattr(lpm, "_is_default_root", lambda root: True)

    calls = []

    def fake_run(cmd, check=False, **kwargs):
        calls.append(cmd)

    monkeypatch.setattr(lpm.subprocess, "run", fake_run)

    lpm.handle_service_files("dummy", root, manifest)

    err = capsys.readouterr().err

    assert "foo.service" in err
    assert "other.service" not in err
    assert calls == [["systemctl", "enable", "--now", "foo.service"]]
