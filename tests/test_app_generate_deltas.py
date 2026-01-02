import os
from pathlib import Path

import pytest

from src.lpm import app
from src.lpm.delta import DeltaMeta


@pytest.fixture
def pkg_meta():
    return app.PkgMeta(name="demo", version="1")


def test_generate_deltas_skips_without_prereqs(tmp_path: Path, monkeypatch, pkg_meta):
    root = tmp_path / "root"
    lpm_dir = root / ".lpm"
    lpm_dir.mkdir(parents=True)

    previous = lpm_dir / "install.sh.old"
    previous.write_text("echo old")
    new_script = lpm_dir / "install.sh"
    new_script.write_text("echo new")

    monkeypatch.setattr(app, "zstd_version", lambda: None)

    manifest: list[dict] = []
    app.generate_deltas(Path("demo.lpm"), pkg_meta, manifest, previous, new_script, root)

    assert not (new_script.with_suffix(".sh.zstpatch")).exists()
    assert manifest == []


def test_generate_deltas_appends_manifest(tmp_path: Path, monkeypatch, pkg_meta):
    root = tmp_path / "root"
    lpm_dir = root / ".lpm"
    lpm_dir.mkdir(parents=True)

    previous = lpm_dir / "install.sh.old"
    previous.write_text("echo old")
    new_script = lpm_dir / "install.sh"
    new_script.write_text("echo new")

    monkeypatch.setattr(app, "zstd_version", lambda: (1, 5, 6))
    monkeypatch.setattr(app, "version_at_least", lambda current, minimum: True)

    def _fake_generate_delta(base, target, output, minimum_version):
        output.write_text("delta")
        return DeltaMeta(
            algorithm="zstd-patch",
            base_version="0",
            base_sha256="base-sha",
            delta_sha256="delta-sha",
            delta_size=os.stat(output).st_size,
            min_tool=f"zstd>={minimum_version}",
        )

    monkeypatch.setattr(app, "generate_delta", _fake_generate_delta)

    manifest: list[dict] = []
    app.generate_deltas(Path("demo.lpm"), pkg_meta, manifest, previous, new_script, root)

    delta_path = new_script.with_suffix(".sh.zstpatch")
    assert delta_path.exists()
    assert manifest
    assert manifest[0]["path"].startswith("/")
    assert manifest[0]["size"] == delta_path.stat().st_size
