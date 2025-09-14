import os, sys, json, shutil, importlib, dataclasses, tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _import_lpm(tmp_path, monkeypatch):
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path/"state"))
    for mod in ["lpm", "src.config"]:
        if mod in sys.modules:
            del sys.modules[mod]
    return importlib.import_module("lpm")


def _make_pkg(lpm, tmp_path, name, requires=None):
    staged = tmp_path / f"stage-{name}"
    staged.mkdir()
    data_file = staged / "file"
    data_file.write_text("content")
    meta = lpm.PkgMeta(name=name, version="1", release="1", arch="noarch", requires=requires or [])
    manifest = [{
        "path": "/file",
        "size": len("content"),
        "sha256": lpm.sha256sum(data_file),
    }]
    (staged/".lpm-meta.json").write_text(json.dumps(dataclasses.asdict(meta)))
    (staged/".lpm-manifest.json").write_text(json.dumps(manifest))
    out = tmp_path / f"{name}.zst"
    with out.open("wb") as f:
        cctx = lpm.zstd.ZstdCompressor()
        with cctx.stream_writer(f) as compressor:
            with tarfile.open(fileobj=compressor, mode="w|") as tf:
                for p in staged.iterdir():
                    tf.add(p, arcname=p.name)
    shutil.rmtree(staged)
    return out


def test_autoremove_removes_unused_dependency(tmp_path, monkeypatch):
    lpm = _import_lpm(tmp_path, monkeypatch)
    monkeypatch.setattr(lpm.shutil, "which", lambda cmd: "/usr/bin/true")
    root = tmp_path / "root"
    root.mkdir()

    dep_pkg = _make_pkg(lpm, tmp_path, "dep")
    pkg_pkg = _make_pkg(lpm, tmp_path, "pkg", ["dep"])

    lpm.installpkg(dep_pkg, root=root, dry_run=False, verify=False, force=False, explicit=False)
    lpm.installpkg(pkg_pkg, root=root, dry_run=False, verify=False, force=False, explicit=True)

    conn = lpm.db()
    row = conn.execute("SELECT requires,explicit FROM installed WHERE name='pkg'").fetchone()
    assert json.loads(row[0]) == ["dep"]
    assert row[1] == 1
    row = conn.execute("SELECT explicit FROM installed WHERE name='dep'").fetchone()
    assert row[0] == 0
    conn.close()

    lpm.do_remove(["pkg"], root=root, dry=False)
    lpm.autoremove(root=root, dry=False)

    conn = lpm.db()
    remaining = conn.execute("SELECT name FROM installed").fetchall()
    conn.close()
    assert remaining == []
