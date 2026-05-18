from __future__ import annotations

from pathlib import Path

from lpm import app as lpm


def test_upgradepkg_parser_routes_to_existing_upgrade_command():
    parser = lpm.build_parser()

    args = parser.parse_args(
        [
            "upgradepkg",
            "demo",
            "--root",
            "/tmp/lpm-root",
            "--dry-run",
            "--no-verify",
            "--no-delta",
            "--allow-fallback",
            "--force",
        ]
    )

    assert args.cmd == "upgradepkg"
    assert args.func is lpm.cmd_upgrade
    assert args.names == ["demo"]
    assert args.root == "/tmp/lpm-root"
    assert args.dry_run is True
    assert args.no_verify is True
    assert args.no_delta is True
    assert args.allow_fallback is True
    assert args.force is True


def test_upgrade_and_upgradepkg_parser_accept_same_relevant_flags():
    parser = lpm.build_parser()
    common = ["demo", "--root", "/tmp/lpm-root", "--dry-run", "--no-verify", "--no-delta", "--no-fallback", "--force"]

    upgrade = parser.parse_args(["upgrade", *common])
    upgradepkg = parser.parse_args(["upgradepkg", *common])

    assert upgradepkg.func is upgrade.func is lpm.cmd_upgrade
    assert upgradepkg.names == upgrade.names == ["demo"]
    assert upgradepkg.root == upgrade.root == "/tmp/lpm-root"
    assert upgradepkg.dry_run == upgrade.dry_run is True
    assert upgradepkg.no_verify == upgrade.no_verify is True
    assert upgradepkg.no_delta == upgrade.no_delta is True
    assert upgradepkg.allow_fallback == upgrade.allow_fallback is False
    assert upgradepkg.force == upgrade.force is True


def test_upgrade_service_cleanup_default_root_uses_privileged_section(monkeypatch):
    calls = []

    @lpm.contextlib.contextmanager
    def fake_privileged_section():
        calls.append("enter")
        yield
        calls.append("exit")

    def fake_remove_service_files(name, root, manifest):
        calls.append(("remove_service_files", name, root, manifest))

    monkeypatch.setattr(lpm, "_is_default_root", lambda root: True)
    monkeypatch.setattr(lpm, "privileges_enabled", lambda: True)
    monkeypatch.setattr(lpm, "privileged_section", fake_privileged_section)
    monkeypatch.setattr(lpm, "remove_service_files", fake_remove_service_files)

    pkg = lpm.PkgMeta(name="demo", version="2.0")
    installed = {"demo": {"version": "1.0", "manifest": [{"path": "/usr/lib/systemd/system/demo.service"}]}}

    lpm._cleanup_upgrade_service_files(pkg, Path(lpm.DEFAULT_ROOT), installed, dry_run=False)

    assert calls == [
        "enter",
        ("remove_service_files", "demo", Path(lpm.DEFAULT_ROOT), installed["demo"]["manifest"]),
        "exit",
    ]


def test_upgrade_service_cleanup_non_default_root_skips_privileged_section(monkeypatch, tmp_path):
    calls = []

    @lpm.contextlib.contextmanager
    def fake_privileged_section():
        calls.append("enter")
        yield
        calls.append("exit")

    def fake_remove_service_files(name, root, manifest):
        calls.append(("remove_service_files", name, root, manifest))

    monkeypatch.setattr(lpm, "_is_default_root", lambda root: False)
    monkeypatch.setattr(lpm, "privileges_enabled", lambda: True)
    monkeypatch.setattr(lpm, "privileged_section", fake_privileged_section)
    monkeypatch.setattr(lpm, "remove_service_files", fake_remove_service_files)

    pkg = lpm.PkgMeta(name="demo", version="2.0")
    installed = {"demo": {"version": "1.0", "manifest": ["/etc/init.d/demo"]}}

    lpm._cleanup_upgrade_service_files(pkg, tmp_path, installed, dry_run=False)

    assert calls == [("remove_service_files", "demo", tmp_path, installed["demo"]["manifest"])]


def test_upgrade_service_cleanup_dry_run_skips_service_removal(monkeypatch):
    calls = []

    @lpm.contextlib.contextmanager
    def fake_privileged_section():
        calls.append("enter")
        yield
        calls.append("exit")

    monkeypatch.setattr(lpm, "_is_default_root", lambda root: True)
    monkeypatch.setattr(lpm, "privileges_enabled", lambda: True)
    monkeypatch.setattr(lpm, "privileged_section", fake_privileged_section)
    monkeypatch.setattr(lpm, "remove_service_files", lambda *_args: calls.append("remove"))

    pkg = lpm.PkgMeta(name="demo", version="2.0")
    installed = {"demo": {"version": "1.0", "manifest": ["/etc/init.d/demo"]}}

    lpm._cleanup_upgrade_service_files(pkg, Path(lpm.DEFAULT_ROOT), installed, dry_run=True)

    assert calls == []
