import importlib
import os
import sys
import subprocess
import textwrap
from pathlib import Path

import pytest

def test_buildpkg_no_deps_skips_dependency_build(tmp_path):
    script = tmp_path / "foo.lpmbuild"
    script.write_text(
        textwrap.dedent(
            """
            NAME=foo
            VERSION=1
            RELEASE=1
            ARCH=noarch
            REQUIRES=(nonexistent-dep)
            prepare() { :; }
            build() { :; }
            staging() {
                mkdir -p "$pkgdir"
                echo hi > "$pkgdir/hi"
            }
            """
        )
    )

    env = os.environ.copy()
    env["LPM_STATE_DIR"] = str(tmp_path / "state")

    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    (stub_dir / "tqdm.py").write_text(
        """
class tqdm:
    def __init__(self, iterable=None, **kwargs):
        self.iterable = iterable or []
        self.n = 0

    def __iter__(self):
        for item in self.iterable:
            self.n += 1
            yield item

    def update(self, n=1):
        self.n += n

    def set_description(self, *args, **kwargs):
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False
"""
    )
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(stub_dir), existing]))

    lpm = Path(__file__).resolve().parent.parent / "lpm.py"

    # Without --no-deps the build should fail trying to fetch the missing dep
    result = subprocess.run(
        [sys.executable, str(lpm), "buildpkg", str(script)],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        input="\n",
    )
    assert result.returncode != 0

    # With --no-deps the build should succeed and produce a package
    result = subprocess.run(
        [sys.executable, str(lpm), "buildpkg", str(script), "--no-deps"],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        input="\n",
    )
    assert result.returncode == 0, result.stderr
    assert any(tmp_path.glob("*.zst"))


def test_run_lpmbuild_defaults_missing_arch(tmp_path, monkeypatch):
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))

    script = tmp_path / "bar.lpmbuild"
    script.write_text(
        textwrap.dedent(
            """
            NAME=bar
            VERSION=1
            RELEASE=1
            prepare() { :; }
            build() { :; }
            staging() {
                mkdir -p "$pkgdir"
                echo hi > "$pkgdir/hi"
            }
            """
        )
    )

    for module in ("lpm", "src.config"):
        sys.modules.pop(module, None)

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    spec = importlib.util.spec_from_file_location("lpm", Path(__file__).resolve().parent.parent / "lpm.py")
    assert spec and spec.loader
    lpm = importlib.util.module_from_spec(spec)
    sys.modules["lpm"] = lpm
    spec.loader.exec_module(lpm)

    built, _, _, splits = lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        prompt_default="n",
        is_dep=False,
        build_deps=False,
    )

    meta, _ = lpm.read_package_meta(built)
    assert meta.arch == lpm.ARCH
    assert splits == []


def test_run_lpmbuild_force_rebuild_builds_installed_dependencies(monkeypatch, tmp_path):
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))

    import lpm as lpm_module

    script = tmp_path / "force.lpmbuild"
    script.write_text("", encoding="utf-8")

    scal = {"NAME": "force", "VERSION": "1", "ARCH": "noarch"}
    arr = {"REQUIRES": ["installed-dep"]}

    monkeypatch.setattr(lpm_module, "_capture_lpmbuild_metadata", lambda path: (scal, arr))

    class DummyConn:
        def close(self):
            return None

    monkeypatch.setattr(lpm_module, "db", lambda: DummyConn())
    monkeypatch.setattr(lpm_module, "db_installed", lambda conn: {"installed-dep": {"provides": []}})
    monkeypatch.setattr(lpm_module, "prompt_install_pkg", lambda *args, **kwargs: None)

    def stop_sandbox(*args, **kwargs):
        raise StopIteration("sandbox")

    monkeypatch.setattr(lpm_module, "sandboxed_run", stop_sandbox)

    fetch_calls = []

    def no_force_fetch(name, dst):
        fetch_calls.append(name)
        raise AssertionError("fetch should not be called without force")

    monkeypatch.setattr(lpm_module, "fetch_lpmbuild", no_force_fetch)

    with pytest.raises(StopIteration, match="sandbox"):
        lpm_module.run_lpmbuild(
            script,
            outdir=tmp_path,
            prompt_install=False,
            build_deps=True,
            force_rebuild=False,
        )

    assert fetch_calls == []

    def force_fetch(name, dst):
        fetch_calls.append(name)
        raise StopIteration("force-fetch")

    monkeypatch.setattr(lpm_module, "fetch_lpmbuild", force_fetch)

    with pytest.raises(StopIteration, match="force-fetch"):
        lpm_module.run_lpmbuild(
            script,
            outdir=tmp_path,
            prompt_install=False,
            build_deps=True,
            force_rebuild=True,
        )

    assert fetch_calls[-1] == "installed-dep"


def test_run_lpmbuild_prompts_use_install_root(monkeypatch, tmp_path):
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))

    import lpm as lpm_module

    script = tmp_path / "install-root.lpmbuild"
    script.write_text("", encoding="utf-8")

    scal = {"NAME": "install-root", "VERSION": "1", "ARCH": "noarch"}
    arr = {"REQUIRES": []}

    monkeypatch.setattr(lpm_module, "_capture_lpmbuild_metadata", lambda path: (scal, arr))
    monkeypatch.setattr(lpm_module, "sandboxed_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(lpm_module, "generate_install_script", lambda stagedir: "exit 0\n")

    built_packages = []

    def _fake_build_package(stagedir, meta, out, sign=True):
        out.write_text("payload", encoding="utf-8")
        built_packages.append(out)

    monkeypatch.setattr(lpm_module, "build_package", _fake_build_package)

    recorded_roots = []

    def _record_prompt(blob, **kwargs):
        recorded_roots.append(kwargs.get("root"))

    monkeypatch.setattr(lpm_module, "prompt_install_pkg", _record_prompt)

    install_root = tmp_path / "dest"

    out, _, _, splits = lpm_module.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=True,
        build_deps=False,
        install_root=install_root,
    )

    assert splits == []
    assert out in built_packages
    assert recorded_roots == [install_root]
