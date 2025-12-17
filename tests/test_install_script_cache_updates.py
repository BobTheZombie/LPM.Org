import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import installgen  # noqa: E402


def test_install_script_skips_root_actions_when_unprivileged(tmp_path):
    staged = tmp_path / "stage"
    modules_dir = staged / "usr/lib64/gio/modules"
    modules_dir.mkdir(parents=True)

    libdir = staged / "usr/lib"
    libdir.mkdir(parents=True)
    (libdir / "libsample.so").write_text("", encoding="utf-8")

    script_path = tmp_path / "install.sh"
    script_path.write_text(installgen.generate_install_script(staged))
    script_path.chmod(0o755)

    root_dir = tmp_path / "root"
    (root_dir / "usr/lib64/gio/modules").mkdir(parents=True)
    (root_dir / "usr/lib").mkdir(parents=True)

    shims = tmp_path / "bin"
    shims.mkdir()
    gio_log = tmp_path / "gio.log"
    ld_log = tmp_path / "ld.log"

    (shims / "id").write_text("#!/bin/sh\necho 1000\n", encoding="utf-8")
    (shims / "gio-querymodules").write_text(
        f"#!/bin/sh\necho \"$@\" >> \"{gio_log}\"\nexit 0\n", encoding="utf-8"
    )
    (shims / "ldconfig").write_text(
        f"#!/bin/sh\necho ldconfig >> \"{ld_log}\"\nexit 0\n", encoding="utf-8"
    )
    for shim in shims.iterdir():
        shim.chmod(0o755)

    env = os.environ.copy()
    env.update({
        "PATH": f"{shims}:{env.get('PATH', '')}",
        "LPM_ROOT": str(root_dir),
    })

    result = subprocess.run(
        [str(script_path), "install"],
        cwd=root_dir,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "gio module cache not refreshed" in result.stderr
    assert f"{root_dir}/usr/lib64/gio/modules" in result.stderr
    assert not gio_log.exists() or gio_log.read_text(encoding="utf-8") == ""
    assert "ldconfig skipped" in result.stderr
    assert not ld_log.exists()

