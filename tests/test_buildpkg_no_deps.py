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
            install() {
                mkdir -p "$pkgdir"
                echo hi > "$pkgdir/hi"
            }
            """
        )
    )

    env = os.environ.copy()
    env["LPM_STATE_DIR"] = str(tmp_path / "state")

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
