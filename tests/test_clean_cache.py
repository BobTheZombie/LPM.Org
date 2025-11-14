import sys, importlib
from pathlib import Path
from types import SimpleNamespace


def _import_lpm(tmp_path, monkeypatch):
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path/"state"))
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    for mod in ["lpm", "lpm.config"]:
        if mod in sys.modules:
            del sys.modules[mod]
    return importlib.import_module("lpm")


def test_clean_cache_removes_only_cached_files(tmp_path, monkeypatch):
    lpm = _import_lpm(tmp_path, monkeypatch)
    cache = lpm.CACHE_DIR
    (cache / "blob1").write_text("data1")
    subdir = cache / "sub"
    subdir.mkdir()
    (subdir / "blob2").write_text("data2")

    other_file = cache.parent / "other.txt"
    other_file.write_text("keep")

    lpm.cmd_clean_cache(SimpleNamespace())

    assert not (cache / "blob1").exists()
    assert not subdir.exists()
    assert other_file.exists()
    assert list(cache.iterdir()) == []
