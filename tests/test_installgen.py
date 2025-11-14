import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lpm.installgen import generate_install_script


def _setup_tools(tmp_path, names):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "log"
    for name in names:
        p = bin_dir / name
        p.write_text(f"#!/bin/sh\necho {name} >> {log}\n")
        p.chmod(0o755)
    return bin_dir, log


def test_generate_install_script_runs_required_commands(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    (stage / "usr/share/applications").mkdir(parents=True)
    (stage / "usr/share/applications/foo.desktop").write_text("[Desktop Entry]")
    (stage / "usr/share/icons/hicolor").mkdir(parents=True)
    (stage / "usr/share/icons/hicolor/index.theme").write_text("[Icon Theme]")
    (stage / "usr/lib").mkdir(parents=True)
    (stage / "usr/lib/libfoo.so").write_text("")

    script = generate_install_script(stage)

    bin_dir, log = _setup_tools(tmp_path, ["update-desktop-database", "gtk-update-icon-cache", "ldconfig"])
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    subprocess.run(["sh", "-c", script], env={**os.environ, "LPM_ROOT": str(stage)}, check=True)

    calls = log.read_text().splitlines()
    assert "update-desktop-database" in calls
    assert "gtk-update-icon-cache" in calls
    assert "ldconfig" not in calls


def test_generate_install_script_skips_missing_commands(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    (stage / "usr/share/applications").mkdir(parents=True)
    (stage / "usr/share/applications/foo.desktop").write_text("[Desktop Entry]")
    (stage / "usr/share/icons/hicolor").mkdir(parents=True)
    (stage / "usr/share/icons/hicolor/index.theme").write_text("[Icon Theme]")

    script = generate_install_script(stage)

    bin_dir, log = _setup_tools(tmp_path, ["gtk-update-icon-cache"])
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    subprocess.run(["sh", "-c", script], env={**os.environ, "LPM_ROOT": str(stage)}, check=True)

    calls = log.read_text().splitlines()
    assert "gtk-update-icon-cache" in calls
    assert "update-desktop-database" not in calls


def test_generate_install_script_ldconfig_only_for_real_root(tmp_path, monkeypatch):
    stage = tmp_path / "stage"
    (stage / "usr/lib").mkdir(parents=True)
    (stage / "usr/lib/libfoo.so.1").write_text("")

    script = generate_install_script(stage)

    bin_dir, log = _setup_tools(tmp_path, ["ldconfig"])
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    subprocess.run(["sh", "-c", script], env={**os.environ, "LPM_ROOT": "/"}, check=True)
    assert "ldconfig" in log.read_text().splitlines()

    log.write_text("")
    subprocess.run(["sh", "-c", script], env={**os.environ, "LPM_ROOT": str(stage)}, check=True)
    assert "ldconfig" not in log.read_text().splitlines()
