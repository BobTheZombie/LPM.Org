import os
import shutil
import sys
import textwrap
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for name in ("zstandard", "tqdm"):
    if name not in sys.modules:
        module = types.ModuleType(name)
        if name == "zstandard":
            class _DummyCompressor:
                def stream_writer(self, fh):
                    return fh

            class _DummyDecompressor:
                def stream_reader(self, fh):
                    return fh

            module.ZstdCompressor = _DummyCompressor
            module.ZstdDecompressor = _DummyDecompressor
        else:
            class _DummyTqdm:
                def __init__(self, iterable=None, total=None, **kwargs):
                    self.iterable = iterable or []
                    self.total = total
                    self.n = 0

                def __iter__(self):
                    for item in self.iterable:
                        self.n += 1
                        yield item

                def update(self, n=1):
                    self.n += n

                def set_description(self, _desc):
                    return None

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

            module.tqdm = _DummyTqdm  # type: ignore[attr-defined]

        sys.modules[name] = module

import lpm


def _write_dummy_lpmbuild(script: Path, deps):
    script.write_text(
        textwrap.dedent(
            """
            NAME=foo
            VERSION=1
            RELEASE=1
            ARCH=noarch
            REQUIRES=({deps})
            prepare() {{ :; }}
            build() {{ :; }}
            install() {{ :; }}
            """
        ).format(deps=" ".join(deps))
    )


def _stub_build_pipeline(monkeypatch):
    monkeypatch.setattr(lpm, "sandboxed_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(lpm, "generate_install_script", lambda stagedir: "echo hi")

    def fake_build_package(stagedir, meta, out, sign=True):
        out.write_text("pkg")

    monkeypatch.setattr(lpm, "build_package", fake_build_package)


def test_run_lpmbuild_caches_installed_lookup(tmp_path, monkeypatch):
    deps = [f"dep{i}" for i in range(5)]
    script = tmp_path / "foo.lpmbuild"
    _write_dummy_lpmbuild(script, deps)

    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    _stub_build_pipeline(monkeypatch)

    calls = {"db": 0, "db_installed": 0, "close": 0}

    class DummyConn:
        def close(self):
            calls["close"] += 1

    def fake_db():
        calls["db"] += 1
        return DummyConn()

    def fake_db_installed(conn):
        calls["db_installed"] += 1
        return {dep: {} for dep in deps}

    monkeypatch.setattr(lpm, "db", fake_db)
    monkeypatch.setattr(lpm, "db_installed", fake_db_installed)

    out_path, _, _, _ = lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=True,
    )

    assert calls == {"db": 1, "db_installed": 1, "close": 1}
    assert out_path.exists()

    out_path.unlink()
    shutil.rmtree(Path("/tmp/pkg-foo"), ignore_errors=True)
    shutil.rmtree(Path("/tmp/build-foo"), ignore_errors=True)
    shutil.rmtree(Path("/tmp/src-foo"), ignore_errors=True)


@pytest.mark.benchmark
def test_run_lpmbuild_dependency_scan_benchmark(tmp_path, monkeypatch):
    deps = [f"dep{i}" for i in range(50)]
    script = tmp_path / "bench.lpmbuild"
    _write_dummy_lpmbuild(script, deps)

    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    _stub_build_pipeline(monkeypatch)

    calls = {"db": 0, "db_installed": 0, "close": 0}

    class DummyConn:
        def close(self):
            calls["close"] += 1

    def fake_db():
        calls["db"] += 1
        return DummyConn()

    def fake_db_installed(conn):
        calls["db_installed"] += 1
        return {dep: {} for dep in deps}

    monkeypatch.setattr(lpm, "db", fake_db)
    monkeypatch.setattr(lpm, "db_installed", fake_db_installed)

    def run():
        out_path, _, _, _ = lpm.run_lpmbuild(
            script,
            outdir=tmp_path,
            prompt_install=False,
            build_deps=True,
        )
        out_path.unlink(missing_ok=True)

    iterations = 5
    durations = []
    for _ in range(iterations):
        start = time.perf_counter()
        run()
        durations.append(time.perf_counter() - start)

    assert calls["db"] == calls["db_installed"] == calls["close"] == iterations
    assert iterations < len(deps)
    assert sum(durations) >= 0

