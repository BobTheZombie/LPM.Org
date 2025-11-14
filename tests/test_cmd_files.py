import sys, importlib, json
from types import SimpleNamespace


def _import_lpm(tmp_path, monkeypatch):
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    for mod in ["lpm", "lpm.config"]:
        if mod in sys.modules:
            del sys.modules[mod]
    return importlib.import_module("lpm")


def test_cmd_files_lists_manifest(tmp_path, monkeypatch, capsys):
    lpm = _import_lpm(tmp_path, monkeypatch)
    conn = lpm.db()
    manifest = ["/a", {"path": "/b"}, "/c"]
    conn.execute(
        "INSERT INTO installed (name,version,release,arch,provides,symbols,requires,manifest,explicit,install_time)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("pkg", "1", "1", "noarch", "[]", "[]", "[]", json.dumps(manifest), 1, 0),
    )
    conn.commit()
    conn.close()

    lpm.cmd_files(SimpleNamespace(name="pkg"))
    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["/a", "/b", "/c"]
