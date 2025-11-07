from argparse import Namespace
from pathlib import Path

import pytest


def test_do_install_aborts_on_failure(monkeypatch, tmp_path):
    from src.lpm import app

    pkg = app.PkgMeta(name="foo", version="1")
    blob_path = tmp_path / "foo.pkg"
    blob_path.write_text("payload", encoding="utf-8")

    monkeypatch.setattr(app, "fetch_all", lambda pkgs: {})

    def failing_installpkg(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(app, "installpkg", failing_installpkg)

    class DummyTxn:
        last = None

        def __init__(self, *args, **kwargs):
            DummyTxn.last = self
            self.events = []
            self.pre = False
            self.post = False

        def add_package_event(self, **kwargs):  # pragma: no cover - not used
            self.events.append(kwargs)

        def ensure_pre_transaction(self):
            self.pre = True

        def run_post_transaction(self):
            self.post = True

    monkeypatch.setattr(app, "HookTransactionManager", DummyTxn)
    monkeypatch.setattr(app, "load_hooks", lambda *_args, **_kwargs: {})

    class DummyConn:
        def close(self):
            pass

    monkeypatch.setattr(app, "db", lambda: DummyConn())
    monkeypatch.setattr(app, "db_installed", lambda conn: {})
    monkeypatch.setattr(app, "read_package_meta", lambda path: (pkg, []))

    with pytest.raises(SystemExit):
        app.do_install(
            [pkg],
            root=tmp_path,
            dry=False,
            verify=False,
            force=False,
            explicit=set(),
            allow_fallback=True,
            local_overrides={pkg.name: blob_path},
        )

    assert DummyTxn.last is not None
    assert DummyTxn.last.pre is True
    assert DummyTxn.last.post is False


def test_cmd_install_restores_snapshot_on_failure(monkeypatch, tmp_path):
    from src.lpm import app

    root = tmp_path / "root"
    root.mkdir()
    state_db = tmp_path / "state.db"
    state_db.write_text("db", encoding="utf-8")
    monkeypatch.setattr(app, "_DB_PATH_OVERRIDE", state_db, raising=False)

    pkg = app.PkgMeta(name="foo", version="1")
    monkeypatch.setattr(app, "build_universe", lambda: object())
    monkeypatch.setattr(app, "solve", lambda goals, u: [pkg])

    blob_path = tmp_path / "foo.pkg"
    blob_path.write_bytes(b"blob")
    monkeypatch.setattr(app, "fetch_blob", lambda meta: (blob_path, None))
    monkeypatch.setattr(app, "read_package_meta", lambda _: (pkg, [{"path": "/etc/foo"}]))

    captured_files: dict[str, list[Path]] = {}

    def fake_create_snapshot(tag, files):
        files = list(files)
        captured_files["files"] = files
        snapshot_path = tmp_path / "snap.tar.zst"
        snapshot_path.write_text("snap", encoding="utf-8")
        return str(snapshot_path)

    monkeypatch.setattr(app, "create_snapshot", fake_create_snapshot)

    class DummyCursor:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    class DummyConn:
        def execute(self, query, params):
            return DummyCursor((123,))

        def close(self):
            pass

    monkeypatch.setattr(app, "db", lambda: DummyConn())

    restored: list[Path] = []

    def fake_restore_snapshot(path):
        restored.append(Path(path))

    monkeypatch.setattr(app, "restore_snapshot", fake_restore_snapshot)

    def failing_do_install(*_args, **_kwargs):
        raise SystemExit(2)

    monkeypatch.setattr(app, "do_install", failing_do_install)

    args = Namespace(
        root=str(root),
        names=["foo"],
        dry_run=False,
        no_verify=False,
        allow_fallback=None,
        force=False,
    )

    with pytest.raises(SystemExit):
        app.cmd_install(args)

    assert restored == [tmp_path / "snap.tar.zst"]
    assert state_db in captured_files["files"]
