from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from lpm import app
from lpm import chroot_helpers


def test_installroot_parser_arguments() -> None:
    parser = app.build_parser()
    args = parser.parse_args([
        "installroot",
        "--root",
        "/tmp/target",
        "--package",
        "base",
        "--manifest",
        "pkgs.txt",
        "--mount-api",
        "--dry-run",
    ])

    assert args.cmd == "installroot"
    assert args.root == "/tmp/target"
    assert args.packages == ["base"]
    assert args.manifest == "pkgs.txt"
    assert args.mount_api is True
    assert args.dry_run is True


def test_run_root_install_generates_expected_command(monkeypatch, tmp_path) -> None:
    calls: list[list[str]] = []

    class Proc:
        returncode = 0

    def fake_run(cmd, check=False):
        calls.append(cmd)
        return Proc()

    monkeypatch.setattr(chroot_helpers.subprocess, "run", fake_run)

    result = chroot_helpers._run_root_install(tmp_path, ["basesystem", "linux"])

    assert calls == [["lpm", "--root", str(tmp_path), "install", "basesystem", "linux"]]
    assert result["installed"] == ["basesystem", "linux"]
    assert result["failed"] == []
    assert result["returncode"] == 0


def test_installroot_dry_run_outputs_structured_summary(tmp_path, capsys) -> None:
    manifest = tmp_path / "manifest.txt"
    manifest.write_text("# comment\nlinux\nvim\n", encoding="utf-8")

    args = SimpleNamespace(
        root=str(tmp_path / "target"),
        cache_dir=str(tmp_path / "cache"),
        packages=["basesystem"],
        manifest=str(manifest),
        mount_api=False,
        dry_run=True,
        verbose=False,
    )

    rc = chroot_helpers.run_installroot(args)

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["target_root"] == str(tmp_path / "target")
    assert payload["packages_requested"] == ["basesystem", "linux", "vim"]
    assert payload["command"] == [
        "lpm",
        "--root",
        str(tmp_path / "target"),
        "install",
        "basesystem",
        "linux",
        "vim",
    ]


def test_installroot_requires_package_or_manifest(tmp_path) -> None:
    args = SimpleNamespace(
        root=str(tmp_path / "target"),
        cache_dir=str(tmp_path / "cache"),
        packages=[],
        manifest=None,
        mount_api=False,
        dry_run=True,
        verbose=False,
    )

    with pytest.raises(ValueError, match="installroot requires --package or --manifest"):
        chroot_helpers.run_installroot(args)
