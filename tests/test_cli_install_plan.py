import dataclasses
import importlib
import json
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _reload_cli_modules(tmp_path, monkeypatch):
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    for name in list(sys.modules):
        if name == "lpm" or name.startswith("lpm."):
            sys.modules.pop(name)

    lpm = importlib.import_module("lpm")
    cli_main = importlib.import_module("lpm.cli.main")
    install_module = importlib.import_module("lpm.cli.commands.install")
    planner_module = importlib.import_module("lpm.cli.planner")
    installpkg_module = importlib.import_module("lpm.installpkg")
    return lpm, cli_main, install_module, planner_module, installpkg_module


def _build_package(lpm, tmp_path, *, name="demo", version="1", release="1"):
    staged = tmp_path / f"stage-{name}-{version}-{release}"
    staged.mkdir()

    payload_path = staged / "foo"
    payload_path.write_text("from package\n")

    manifest = lpm.collect_manifest(staged)
    meta = lpm.PkgMeta(name=name, version=version, release=release, arch="noarch")

    (staged / ".lpm-meta.json").write_text(json.dumps(dataclasses.asdict(meta)))
    (staged / ".lpm-manifest.json").write_text(json.dumps(manifest))

    out = tmp_path / f"{name}-{version}-{release}.zst"
    with out.open("wb") as f:
        cctx = lpm.zstd.ZstdCompressor()
        with cctx.stream_writer(f) as compressor:
            with tarfile.open(fileobj=compressor, mode="w|") as tf:
                for p in staged.iterdir():
                    tf.add(p, arcname=p.name)

    shutil.rmtree(staged)
    return out


def _add_trusted_key(pkg_path: Path, trust_dir: Path) -> None:
    trust_dir.mkdir(parents=True, exist_ok=True)
    key_dir = trust_dir.parent / "keys"
    key_dir.mkdir(parents=True, exist_ok=True)

    priv = key_dir / "signing.pem"
    pub = trust_dir / "signing.pem"

    subprocess.run(["openssl", "genrsa", "-out", str(priv), "2048"], check=True)
    subprocess.run(
        ["openssl", "rsa", "-in", str(priv), "-pubout", "-out", str(pub)], check=True
    )
    subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-sign",
            str(priv),
            "-out",
            str(pkg_path) + ".sig",
            str(pkg_path),
        ],
        check=True,
    )


def test_cli_install_plan_executes_install(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()

    lpm, cli_main, install_module, planner_module, installpkg_module = _reload_cli_modules(
        tmp_path, monkeypatch
    )

    monkeypatch.setattr(install_module, "require_root", lambda intent=None: None)
    monkeypatch.setattr(installpkg_module, "ensure_root_or_escalate", lambda intent=None: None)

    trust_dir = tmp_path / "trust"
    monkeypatch.setattr(lpm.config, "TRUST_DIR", trust_dir)
    monkeypatch.setattr(lpm.app, "TRUST_DIR", trust_dir)
    monkeypatch.setattr(installpkg_module._app, "TRUST_DIR", trust_dir)

    pkg = _build_package(lpm, tmp_path)
    _add_trusted_key(pkg, trust_dir)

    plan = planner_module.build_install_plan([str(pkg)], root=root)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))

    exit_code = cli_main.main(["install", "--plan", str(plan_path)])
    assert exit_code == 0

    installed_payload = root / "foo"
    assert installed_payload.read_text() == "from package\n"

    conn = sqlite3.connect(lpm.config.DB_PATH)
    try:
        row = conn.execute(
            "SELECT name, version, release, explicit FROM installed WHERE name=?", ("demo",)
        ).fetchone()
    finally:
        conn.close()

    assert row == ("demo", "1", "1", 1)


def test_install_command_reports_privileged_exit(tmp_path, monkeypatch, capsys):
    _, cli_main, install_module, _, _ = _reload_cli_modules(tmp_path, monkeypatch)

    monkeypatch.setattr(install_module, "require_root", lambda intent=None: None)

    def boom(_plan):
        raise SystemExit(12)

    monkeypatch.setattr(install_module, "apply_install_plan", boom)

    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps({"packages": []}))

    exit_code = cli_main.main(["install", "--plan", str(plan_path)])
    captured = capsys.readouterr()

    assert exit_code == 12
    assert "exit code 12" in captured.err
