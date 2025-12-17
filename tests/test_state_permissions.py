import importlib
import sys
from pathlib import Path


def _reimport_lpm(tmp_path):
    for mod in ("lpm", "lpm.app", "src.lpm", "src.lpm.app", "src.config", "config"):
        if mod in sys.modules:
            del sys.modules[mod]
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    importlib.invalidate_caches()
    importlib.import_module("src")
    return importlib.import_module("src.lpm.app")


def test_state_db_is_group_writable(tmp_path, monkeypatch):
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    lpm_app = _reimport_lpm(tmp_path)

    conn = lpm_app.db()
    conn.execute("SELECT 1")
    conn.close()

    state_dir = tmp_path / "state"
    db_path = state_dir / "state.db"

    assert (state_dir.stat().st_mode & 0o777) == 0o775
    assert (db_path.stat().st_mode & 0o777) == 0o664

