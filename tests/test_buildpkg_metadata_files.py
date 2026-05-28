import dataclasses
import json
import os
import stat
from pathlib import Path

import lpm.app as app


def _meta() -> app.PkgMeta:
    return app.PkgMeta(name="demo", version="1.0", release="1", arch="noarch")


def _fake_run(*args, **kwargs):
    return None


def test_build_package_writes_metadata_files_with_expected_content(monkeypatch, tmp_path):
    stagedir = tmp_path / "stage"
    stagedir.mkdir()
    (stagedir / "usr").mkdir()
    (stagedir / "usr" / "hello.txt").write_text("hello\n", encoding="utf-8")

    monkeypatch.setattr(app.shutil, "which", lambda cmd: "zstd")
    monkeypatch.setattr(app.subprocess, "run", _fake_run)

    meta = _meta()
    app.build_package(stagedir, meta, tmp_path / "demo.zst", sign=False)

    meta_path = stagedir / ".lpm-meta.json"
    mani_path = stagedir / ".lpm-manifest.json"

    assert json.loads(meta_path.read_text(encoding="utf-8"))["name"] == "demo"
    assert json.loads(mani_path.read_text(encoding="utf-8"))[0]["path"] == "/usr/hello.txt"


def test_build_package_metadata_permissions_ignore_umask(monkeypatch, tmp_path):
    stagedir = tmp_path / "stage"
    stagedir.mkdir()
    (stagedir / "payload.txt").write_text("payload", encoding="utf-8")

    monkeypatch.setattr(app.shutil, "which", lambda cmd: "zstd")
    monkeypatch.setattr(app.subprocess, "run", _fake_run)

    previous_umask = os.umask(0o077)
    try:
        app.build_package(stagedir, _meta(), tmp_path / "demo.zst", sign=False)
    finally:
        os.umask(previous_umask)

    meta_mode = stat.S_IMODE((stagedir / ".lpm-meta.json").stat().st_mode)
    mani_mode = stat.S_IMODE((stagedir / ".lpm-manifest.json").stat().st_mode)

    assert meta_mode == 0o644
    assert mani_mode == 0o644
