import importlib
import sys
from pathlib import Path


def _import_lpm(tmp_path, monkeypatch):
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    for mod in ["lpm", "src.config"]:
        if mod in sys.modules:
            del sys.modules[mod]
    return importlib.import_module("lpm")


def test_maybe_fetch_source_uses_cache(tmp_path, monkeypatch):
    lpm = _import_lpm(tmp_path, monkeypatch)
    calls = {"count": 0}

    def fake_urlread(url):
        calls["count"] += 1
        return b"payload"

    monkeypatch.setattr(lpm, "urlread", fake_urlread)
    monkeypatch.setattr(lpm, "ok", lambda msg: None)
    monkeypatch.setattr(lpm, "warn", lambda msg: None)

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    url = "https://example.com/sources/foo.tar.gz"

    lpm._maybe_fetch_source(url, src_dir)
    dst = src_dir / "foo.tar.gz"
    assert dst.exists()
    assert dst.read_bytes() == b"payload"
    assert calls["count"] == 1

    dst.unlink()

    lpm._maybe_fetch_source(url, src_dir)
    assert dst.exists()
    assert dst.read_bytes() == b"payload"
    assert calls["count"] == 1

    cache_files = list(Path(lpm.SOURCE_CACHE_DIR).glob("foo.tar-*.gz"))
    assert cache_files, "cached source should exist"
    assert cache_files[0].read_bytes() == b"payload"
