import contextlib
import importlib
import sys
import types
from pathlib import Path


def _import_lpm(tmp_path, monkeypatch):
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    for mod in ["lpm", "src.config"]:
        if mod in sys.modules:
            del sys.modules[mod]
    class DummyTqdm:
        def __init__(self, iterable=None, **kwargs):
            self.iterable = iterable
            self.n = 0
            self.total = kwargs.get("total")
            self.desc = kwargs.get("desc")

        def __iter__(self):
            if self.iterable is None:
                return iter(())
            for item in self.iterable:
                self.n += 1
                yield item

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def update(self, value):
            self.n += value

        def set_description(self, desc):
            self.desc = desc

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "tqdm", types.SimpleNamespace(tqdm=DummyTqdm))
    dummy_module = types.SimpleNamespace(
        ZstdCompressor=lambda *args, **kwargs: types.SimpleNamespace(
            stream_writer=lambda f: contextlib.nullcontext(f)
        ),
        ZstdDecompressor=lambda *args, **kwargs: types.SimpleNamespace(
            stream_reader=lambda f: contextlib.nullcontext(f)
        ),
    )
    monkeypatch.setitem(sys.modules, "zstandard", dummy_module)
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
