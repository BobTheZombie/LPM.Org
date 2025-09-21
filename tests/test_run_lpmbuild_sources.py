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


def _write_dummy_lpmbuild(path: Path):
    path.write_text(
        "prepare() { :; }\n"
        "build() { :; }\n"
        "install() { :; }\n",
        encoding="utf-8",
    )


def _base_scalars():
    return {
        "NAME": "foo",
        "VERSION": "1.0",
        "RELEASE": "1",
        "ARCH": "noarch",
        "SUMMARY": "",
        "URL": "https://example.com/fallback.tar.gz",
        "LICENSE": "",
    }


def _empty_arrays():
    keys = [
        "SOURCE",
        "REQUIRES",
        "PROVIDES",
        "CONFLICTS",
        "OBSOLETES",
        "RECOMMENDS",
        "SUGGESTS",
    ]
    return {key: [] for key in keys}


def test_run_lpmbuild_fetches_sources_from_array(tmp_path, monkeypatch):
    lpm = _import_lpm(tmp_path, monkeypatch)

    script = tmp_path / "foo.lpmbuild"
    _write_dummy_lpmbuild(script)

    scalars = _base_scalars()
    arrays = _empty_arrays()
    arrays["SOURCE"] = [
        "https://example.com/foo.tar.gz",
        "pkg::https://example.com/bar.tar.gz",
        "git+https://example.com/repo.tar.gz",
        "ignored-local.patch",
    ]

    monkeypatch.setattr(lpm, "_capture_lpmbuild_metadata", lambda _: (scalars, arrays))
    monkeypatch.setattr(lpm, "sandboxed_run", lambda *args, **kwargs: None)

    fetched = []

    def fake_fetch(url, dest):
        fetched.append((url, dest))

    monkeypatch.setattr(lpm, "_maybe_fetch_source", fake_fetch)

    def fake_build_package(stagedir, meta, out, sign=True):
        out.write_text("dummy", encoding="utf-8")

    monkeypatch.setattr(lpm, "build_package", fake_build_package)

    out, duration, phases, records = lpm.run_lpmbuild(
        script,
        tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    assert out.exists()
    assert duration >= 0
    assert phases == 3
    assert records == []

    fetched_urls = [url for url, _ in fetched]
    assert fetched_urls == [
        "https://example.com/foo.tar.gz",
        "https://example.com/bar.tar.gz",
        "https://example.com/repo.tar.gz",
    ]


def test_run_lpmbuild_falls_back_to_url(tmp_path, monkeypatch):
    lpm = _import_lpm(tmp_path, monkeypatch)

    script = tmp_path / "foo.lpmbuild"
    _write_dummy_lpmbuild(script)

    scalars = _base_scalars()
    arrays = _empty_arrays()

    monkeypatch.setattr(lpm, "_capture_lpmbuild_metadata", lambda _: (scalars, arrays))
    monkeypatch.setattr(lpm, "sandboxed_run", lambda *args, **kwargs: None)

    fetched = []

    def fake_fetch(url, dest):
        fetched.append((url, dest))

    monkeypatch.setattr(lpm, "_maybe_fetch_source", fake_fetch)

    def fake_build_package(stagedir, meta, out, sign=True):
        out.write_text("dummy", encoding="utf-8")

    monkeypatch.setattr(lpm, "build_package", fake_build_package)

    lpm.run_lpmbuild(
        script,
        tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    assert len(fetched) == 1
    fetched_url, fetched_path = fetched[0]
    assert fetched_url == "https://example.com/fallback.tar.gz"
    assert fetched_path.is_dir()
