import importlib
import os
import sys
import subprocess
import textwrap
from pathlib import Path

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
