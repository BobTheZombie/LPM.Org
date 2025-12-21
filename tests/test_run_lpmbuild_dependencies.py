import os
import shlex
import shutil
import sys
import textwrap
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if "tqdm" not in sys.modules:
    import types

    module = types.ModuleType("tqdm")

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

    sys.modules["tqdm"] = module

import lpm


def _write_dummy_lpmbuild(
    script: Path,
    deps,
    python_deps=None,
    build_deps=None,
    *,
    name: str = "foo",
):
    python_block = ""
    if python_deps:
        python_block = f"REQUIRES_PYTHON_DEPENDENCIES=({' '.join(python_deps)})\n"
    build_block = ""
    if build_deps:
        build_block = f"BUILD_REQUIRES=({' '.join(shlex.quote(dep) for dep in build_deps)})\n"
    deps_str = " ".join(shlex.quote(dep) for dep in deps)
    script.write_text(
        textwrap.dedent(
            """
            NAME={name}
            VERSION=1
            RELEASE=1
            ARCH=noarch
            REQUIRES=({deps})
            {build_block}{python_block}prepare() {{ :; }}
            build() {{ :; }}
            staging() {{ :; }}
            """
        ).format(deps=deps_str, build_block=build_block, python_block=python_block, name=name)
    )


def _stub_build_pipeline(monkeypatch):
    monkeypatch.setattr(lpm, "sandboxed_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(lpm, "generate_install_script", lambda stagedir: "echo hi")
    import src.lpm.app as lpm_app

    def fake_read_package_meta(blob):
        return (lpm.PkgMeta(name=blob.name.split("-")[0], version="1", arch="noarch"), [])

    monkeypatch.setattr(lpm, "read_package_meta", fake_read_package_meta)
    monkeypatch.setattr(lpm_app, "read_package_meta", fake_read_package_meta)
    import sys

    sys.modules["lpm"] = lpm
    sys.modules["lpm"].read_package_meta = fake_read_package_meta
    assert lpm.read_package_meta is fake_read_package_meta
    assert lpm_app.read_package_meta is fake_read_package_meta

    def fake_resolve(name, default):
        if name == "read_package_meta":
            return fake_read_package_meta
        return default

    monkeypatch.setattr(lpm, "_resolve_lpm_attr", fake_resolve)
    monkeypatch.setattr(lpm_app, "_resolve_lpm_attr", fake_resolve)

    def fake_build_package(stagedir, meta, out, sign=True):
        out.write_text("pkg")

    monkeypatch.setattr(lpm, "build_package", fake_build_package)


def test_run_lpmbuild_builds_python_dependencies_when_missing(tmp_path, monkeypatch):
    script = tmp_path / "foo.lpmbuild"
    _write_dummy_lpmbuild(script, [], python_deps=["requests==2.0"])

    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    _stub_build_pipeline(monkeypatch)

    class DummyConn:
        def close(self):
            return None

    monkeypatch.setattr(lpm, "db", lambda: DummyConn())
    monkeypatch.setattr(lpm, "db_installed", lambda conn: {})

    calls = []

    def fake_build_python_package_from_pip(spec, outdir, include_deps, cpu_overrides=None):
        calls.append((spec, Path(outdir), include_deps, cpu_overrides))
        out_path = Path(outdir) / "python-requests-2.0-1.noarch.zst"
        out_path.write_text("pkg")
        meta = lpm.PkgMeta(
            name="python-requests",
            version="2.0",
            release="1",
            arch="noarch",
            provides=["pypi(requests)"],
        )
        return out_path, meta, 0.1

    monkeypatch.setattr(lpm, "build_python_package_from_pip", fake_build_python_package_from_pip)

    out_path, _, _, _ = lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=True,
    )

    assert calls == [("requests==2.0", tmp_path, True, None)]
    assert out_path.exists()

    out_path.unlink()
    shutil.rmtree(Path("/tmp/pkg-foo"), ignore_errors=True)
    shutil.rmtree(Path("/tmp/build-foo"), ignore_errors=True)
    shutil.rmtree(Path("/tmp/src-foo"), ignore_errors=True)


def test_run_lpmbuild_builds_python_alias_dependencies(tmp_path, monkeypatch):
    script = tmp_path / "foo.lpmbuild"
    _write_dummy_lpmbuild(script, [], python_deps=["python-docutils"])

    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    _stub_build_pipeline(monkeypatch)

    class DummyConn:
        def close(self):
            return None

    monkeypatch.setattr(lpm, "db", lambda: DummyConn())
    monkeypatch.setattr(lpm, "db_installed", lambda conn: {})

    calls = []

    def fake_build_python_package_from_pip(spec, outdir, include_deps, cpu_overrides=None):
        calls.append((spec, Path(outdir), include_deps, cpu_overrides))
        assert spec == "python-docutils"
        out_path = Path(outdir) / "python-docutils-1-1.noarch.zst"
        out_path.write_text("pkg")
        meta = lpm.PkgMeta(
            name="python-docutils",
            version="1",
            release="1",
            arch="noarch",
            provides=["pypi(docutils)"],
        )
        return out_path, meta, 0.1

    monkeypatch.setattr(lpm, "build_python_package_from_pip", fake_build_python_package_from_pip)

    out_path, _, _, _ = lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=True,
    )

    assert calls == [("python-docutils", tmp_path, True, None)]
    assert out_path.exists()

    out_path.unlink()
    shutil.rmtree(Path("/tmp/pkg-foo"), ignore_errors=True)
    shutil.rmtree(Path("/tmp/build-foo"), ignore_errors=True)
    shutil.rmtree(Path("/tmp/src-foo"), ignore_errors=True)


def test_run_lpmbuild_skips_python_dependencies_when_provided(tmp_path, monkeypatch):
    script = tmp_path / "foo.lpmbuild"
    _write_dummy_lpmbuild(script, [], python_deps=["requests==2.0"])

    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    _stub_build_pipeline(monkeypatch)

    class DummyConn:
        def close(self):
            return None

    monkeypatch.setattr(lpm, "db", lambda: DummyConn())
    monkeypatch.setattr(
        lpm,
        "db_installed",
        lambda conn: {"python-requests": {"provides": ["pypi(requests)"]}},
    )

    def fake_build_python_package_from_pip(*args, **kwargs):
        raise AssertionError("unexpected Python dependency build")

    monkeypatch.setattr(lpm, "build_python_package_from_pip", fake_build_python_package_from_pip)

    out_path, _, _, _ = lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=True,
    )

    assert out_path.exists()

    out_path.unlink()
    shutil.rmtree(Path("/tmp/pkg-foo"), ignore_errors=True)
    shutil.rmtree(Path("/tmp/build-foo"), ignore_errors=True)
    shutil.rmtree(Path("/tmp/src-foo"), ignore_errors=True)


def test_run_lpmbuild_skips_python_dependencies_when_installed(tmp_path, monkeypatch):
    script = tmp_path / "foo.lpmbuild"
    _write_dummy_lpmbuild(script, [], python_deps=["python-docutils"])

    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    _stub_build_pipeline(monkeypatch)

    class DummyConn:
        def close(self):
            return None

    monkeypatch.setattr(lpm, "db", lambda: DummyConn())
    monkeypatch.setattr(
        lpm,
        "db_installed",
        lambda conn: {"python-docutils": {"provides": []}},
    )

    def fake_build_python_package_from_pip(*args, **kwargs):
        raise AssertionError("unexpected Python dependency build")

    monkeypatch.setattr(lpm, "build_python_package_from_pip", fake_build_python_package_from_pip)

    out_path, _, _, _ = lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=True,
    )

    assert out_path.exists()

    out_path.unlink()
    shutil.rmtree(Path("/tmp/pkg-foo"), ignore_errors=True)
    shutil.rmtree(Path("/tmp/build-foo"), ignore_errors=True)
    shutil.rmtree(Path("/tmp/src-foo"), ignore_errors=True)


def test_run_lpmbuild_skips_dependencies_satisfied_by_provides(tmp_path, monkeypatch):
    script = tmp_path / "foo.lpmbuild"
    _write_dummy_lpmbuild(script, ["virtual-pkg"])

    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    _stub_build_pipeline(monkeypatch)

    class DummyConn:
        def close(self):
            return None

    def fake_db():
        return DummyConn()

    def fake_db_installed(conn):
        return {
            "provider": {
                "provides": ["virtual-pkg=1.0"],
            }
        }

    monkeypatch.setattr(lpm, "db", fake_db)
    monkeypatch.setattr(lpm, "db_installed", fake_db_installed)

    def fake_fetch_lpmbuild(pkgname: str, dest: Path) -> Path:
        raise AssertionError(f"unexpected dependency fetch: {pkgname}")

    monkeypatch.setattr(lpm, "fetch_lpmbuild", fake_fetch_lpmbuild)

    out_path, _, _, _ = lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=True,
    )

    assert out_path.exists()

    out_path.unlink()
    shutil.rmtree(Path("/tmp/pkg-foo"), ignore_errors=True)
    shutil.rmtree(Path("/tmp/build-foo"), ignore_errors=True)
    shutil.rmtree(Path("/tmp/src-foo"), ignore_errors=True)


def test_run_lpmbuild_respects_pkgconfig_capabilities(tmp_path, monkeypatch):
    script = tmp_path / "foo.lpmbuild"
    _write_dummy_lpmbuild(script, ["pkgconfig(bar)"])

    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    _stub_build_pipeline(monkeypatch)

    class DummyConn:
        def close(self):
            return None

    monkeypatch.setattr(lpm, "db", lambda: DummyConn())
    monkeypatch.setattr(
        lpm,
        "db_installed",
        lambda conn: {"bar": {"provides": ["pkgconfig(bar)"]}},
    )

    def fake_fetch_lpmbuild(pkgname: str, dest: Path) -> Path:
        raise AssertionError(f"unexpected dependency fetch: {pkgname}")

    monkeypatch.setattr(lpm, "fetch_lpmbuild", fake_fetch_lpmbuild)

    out_path, _, _, _ = lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=True,
    )

    assert out_path.exists()

    out_path.unlink()
    shutil.rmtree(Path("/tmp/pkg-foo"), ignore_errors=True)
    shutil.rmtree(Path("/tmp/build-foo"), ignore_errors=True)
    shutil.rmtree(Path("/tmp/src-foo"), ignore_errors=True)


def test_run_lpmbuild_builds_build_requires(tmp_path, monkeypatch):
    script = tmp_path / "foo.lpmbuild"
    _write_dummy_lpmbuild(script, [], build_deps=["bar"], name="foo")

    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    _stub_build_pipeline(monkeypatch)
    import src.lpm.app as lpm_app

    built = []

    def fake_build_package(stagedir, meta, out, sign=True):
        built.append(meta.name)
        out.write_text("pkg")

    monkeypatch.setattr(lpm, "build_package", fake_build_package)
    monkeypatch.setattr(lpm_app, "build_package", fake_build_package)

    class DummyConn:
        def close(self):
            return None

    monkeypatch.setattr(lpm, "db", lambda: DummyConn())
    monkeypatch.setattr(lpm, "db_installed", lambda conn: {})

    def fake_fetch_lpmbuild(pkgname: str, dest: Path) -> Path:
        _write_dummy_lpmbuild(dest, [], name=pkgname)
        return dest

    monkeypatch.setattr(lpm, "fetch_lpmbuild", fake_fetch_lpmbuild)

    main_script = script.resolve()
    orig_run_lpmbuild = lpm.run_lpmbuild

    def fake_run_lpmbuild(script_path, *args, **kwargs):
        if Path(script_path).resolve() != main_script:
            dep_name = Path(script_path).stem
            if dep_name.startswith("lpm-dep-"):
                dep_name = dep_name[len("lpm-dep-") :]
            built.append(dep_name)
            return None, 0.0, 0, []
        return orig_run_lpmbuild(script_path, *args, **kwargs)

    monkeypatch.setattr(lpm, "run_lpmbuild", fake_run_lpmbuild)
    monkeypatch.setattr(lpm_app, "run_lpmbuild", fake_run_lpmbuild)

    out_path, _, _, _ = lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=True,
    )

    assert out_path.exists()
    assert built.count("foo") == 1
    assert built.count("bar") == 1

    out_path.unlink()
    shutil.rmtree(Path("/tmp/pkg-foo"), ignore_errors=True)
    shutil.rmtree(Path("/tmp/build-foo"), ignore_errors=True)
    shutil.rmtree(Path("/tmp/src-foo"), ignore_errors=True)


def test_run_lpmbuild_detects_dependency_cycle(tmp_path, monkeypatch, capsys):
    cycle_scripts = {
        "foo": tmp_path / "foo.lpmbuild",
        "bar": tmp_path / "bar.lpmbuild",
    }

    cycle_scripts["foo"].write_text(
        textwrap.dedent(
            """
            NAME=foo
            VERSION=1
            RELEASE=1
            ARCH=noarch
            REQUIRES=('bar')
            prepare() { :; }
            build() { :; }
            staging() { :; }
            """
        )
    )
    cycle_scripts["bar"].write_text(
        textwrap.dedent(
            """
            NAME=bar
            VERSION=1
            RELEASE=1
            ARCH=noarch
            REQUIRES=('foo')
            prepare() { :; }
            build() { :; }
            staging() { :; }
            """
        )
    )

    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    _stub_build_pipeline(monkeypatch)

    class DummyConn:
        def close(self):
            return None

    monkeypatch.setattr(lpm, "db", lambda: DummyConn())
    monkeypatch.setattr(lpm, "db_installed", lambda conn: {})

    def fake_fetch_lpmbuild(pkgname: str, dest: Path) -> Path:
        src = cycle_scripts.get(pkgname)
        if src is None:
            raise AssertionError(f"unexpected dependency fetch: {pkgname}")
        shutil.copy2(src, dest)
        return dest

    monkeypatch.setattr(lpm, "fetch_lpmbuild", fake_fetch_lpmbuild)

    with pytest.raises(SystemExit) as exc:
        lpm.run_lpmbuild(
            cycle_scripts["foo"],
            outdir=tmp_path,
            prompt_install=False,
            build_deps=True,
        )

    assert exc.value.code == 2
    err = capsys.readouterr().err.lower()
    assert "dependency cycle detected" in err
    assert "foo -> bar -> foo" in err


def test_run_lpmbuild_collects_dependency_arrays(tmp_path, monkeypatch):
    script = tmp_path / "arrays.lpmbuild"
    script.write_text(
        textwrap.dedent(
            """
            NAME=toolchain
            VERSION=1
            RELEASE=2
            ARCH=noarch
            REQUIRES=(
              'glibc'
              'linux-headers'
              'binutils'
              'zlib'
            )
            BUILD_REQUIRES=(
              'git'
              'base-devel'
            )
            PROVIDES=('gcc' 'cc-bin')
            CONFLICTS=('gcc-old' 'gcc-beta')
            OBSOLETES=('gcc-12')
            RECOMMENDS=('gdb')
            SUGGESTS=('valgrind')
            prepare() { :; }
            build() { :; }
            staging() {
                mkdir -p "$pkgdir"
                echo hi > "$pkgdir/hi"
            }
            """
        )
    )

    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(lpm, "sandboxed_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(lpm, "generate_install_script", lambda stagedir: "echo hi")

    captured = {}

    def fake_build_package(stagedir, meta, out, sign=True):
        captured["meta"] = meta
        out.write_text("pkg")

    monkeypatch.setattr(lpm, "build_package", fake_build_package)

    out_path, _, _, _ = lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    assert out_path.exists()
    meta = captured["meta"]
    assert meta.requires == ["glibc", "linux-headers", "binutils", "zlib"]
    assert meta.build_requires == ["git", "base-devel"]
    assert meta.provides == ["gcc", "cc-bin"]
    assert meta.conflicts == ["gcc-old", "gcc-beta"]
    assert meta.obsoletes == ["gcc-12"]
    assert meta.recommends == ["gdb"]
    assert meta.suggests == ["valgrind"]


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

