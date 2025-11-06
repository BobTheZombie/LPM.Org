from __future__ import annotations

import importlib
import os
import sqlite3

import pytest


def _reload_config():
    config = importlib.import_module("src.config")
    return importlib.reload(config)


def _reload_locking():
    locking = importlib.import_module("src.lpm.locking")
    return importlib.reload(locking)


def _reload_app():
    app = importlib.import_module("src.lpm.app")
    return importlib.reload(app)


@pytest.fixture
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LPM_LOCK_PATH", str(tmp_path / "lock"))
    cfg = _reload_config()
    lock = _reload_locking()
    app = _reload_app()
    try:
        yield cfg, lock, app
    finally:
        monkeypatch.delenv("LPM_STATE_DIR", raising=False)
        monkeypatch.delenv("LPM_LOCK_PATH", raising=False)
        _reload_config()
        _reload_locking()
        _reload_app()


def test_transaction_lock_blocks_concurrent_attempts(isolated_state, capsys):
    cfg, locking, app = isolated_state

    with locking.global_transaction_lock():
        with pytest.raises(locking.TransactionLockError) as exc:
            with locking.global_transaction_lock():
                pass

    assert "another transaction is running" in str(exc.value)
    assert str(os.getpid()) in str(exc.value)


def test_transaction_context_rejects_when_locked(isolated_state, capsys):
    cfg, locking, app = isolated_state

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")

    with locking.global_transaction_lock():
        with pytest.raises(SystemExit) as exc:
            with app.transaction(conn, "test", dry=False):
                conn.execute("INSERT INTO t (id) VALUES (1)")

    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "another transaction is running" in captured.err
