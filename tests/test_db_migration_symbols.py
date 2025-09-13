import os
import sys
import sqlite3
import subprocess
import textwrap
from pathlib import Path


def test_buildpkg_handles_old_db_without_symbols(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    db_path = state_dir / "state.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE installed(
            name TEXT PRIMARY KEY,
            version TEXT NOT NULL,
            release TEXT NOT NULL,
            arch TEXT NOT NULL,
            provides TEXT NOT NULL,
            manifest TEXT NOT NULL,
            install_time INTEGER NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO installed(name,version,release,arch,provides,manifest,install_time) VALUES(?,?,?,?,?,?,?)",
        ("base", "1", "1", "x86_64", "[]", "[]", 0),
    )
    conn.commit()
    conn.close()

    script = tmp_path / "foo.lpmbuild"
    script.write_text(
        textwrap.dedent(
            """
            NAME=foo
            VERSION=1
            RELEASE=1
            REQUIRES=(base)
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
    env["LPM_STATE_DIR"] = str(state_dir)
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent.parent / "lpm.py"), "buildpkg", str(script)],
        env=env,
        capture_output=True,
        text=True,
        input="\n",
    )
    assert result.returncode == 0, result.stderr
