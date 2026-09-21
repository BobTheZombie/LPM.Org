from __future__ import annotations

import json
from pathlib import Path

from lpm import app as lpm


def _seed_package(conn, name: str, version: str, manifest: list[dict]) -> None:
    conn.execute(
        "INSERT INTO installed(name,version,release,arch,provides,symbols,requires,manifest,explicit,install_time) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (name, version, "1", "x86_64", json.dumps([name]), "[]", "[]", json.dumps(manifest), 1, 1),
    )
    conn.commit()


def test_rollback_restores_files_removes_new_paths_and_database(tmp_path, monkeypatch):
    state = tmp_path / "state"
    root = tmp_path / "root"
    versions = state / "versions"
    root.mkdir()
    monkeypatch.setenv("LPM_STATE_DIR", str(state))
    monkeypatch.setattr(lpm, "VERSION_STORE_DIR", versions)

    old_file = root / "usr/bin/demo"
    old_file.parent.mkdir(parents=True)
    old_file.write_text("version one", encoding="utf-8")
    new_path = root / "usr/share/demo/new.dat"

    conn = lpm.db()
    _seed_package(conn, "demo", "1.0", [{"path": "/usr/bin/demo"}])
    conn.close()

    archive = lpm.create_snapshot(
        "upgrade-demo",
        [old_file, new_path],
        root=root,
        packages=["demo"],
    )

    old_file.write_text("version two", encoding="utf-8")
    new_path.parent.mkdir(parents=True)
    new_path.write_text("new", encoding="utf-8")
    conn = lpm.db()
    conn.execute("UPDATE installed SET version=?, manifest=? WHERE name=?", (
        "2.0", json.dumps([{"path": "/usr/bin/demo"}, {"path": "/usr/share/demo/new.dat"}]), "demo"
    ))
    conn.commit()
    conn.close()

    lpm.restore_snapshot(Path(archive), root=root)

    assert old_file.read_text(encoding="utf-8") == "version one"
    assert not new_path.exists()
    conn = lpm.db()
    row = conn.execute("SELECT version,manifest FROM installed WHERE name='demo'").fetchone()
    conn.close()
    assert row[0] == "1.0"
    assert json.loads(row[1]) == [{"path": "/usr/bin/demo"}]


def test_rollback_removes_newly_installed_package_record(tmp_path, monkeypatch):
    state = tmp_path / "state"
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("LPM_STATE_DIR", str(state))
    monkeypatch.setattr(lpm, "VERSION_STORE_DIR", state / "versions")
    payload = root / "usr/bin/fresh"

    archive = lpm.create_snapshot("install-fresh", [payload], root=root, packages=["fresh"])
    payload.parent.mkdir(parents=True)
    payload.write_text("fresh", encoding="utf-8")
    conn = lpm.db()
    _seed_package(conn, "fresh", "1.0", [{"path": "/usr/bin/fresh"}])
    conn.close()

    lpm.restore_snapshot(Path(archive), root=root)

    assert not payload.exists()
    conn = lpm.db()
    assert conn.execute("SELECT 1 FROM installed WHERE name='fresh'").fetchone() is None
    conn.close()
