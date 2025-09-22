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
            staging() {
                mkdir -p "$pkgdir"
                echo hi > "$pkgdir/hi"
            }
            """
        )
    )

    env = os.environ.copy()
    env["LPM_STATE_DIR"] = str(state_dir)

    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    (stub_dir / "zstandard.py").write_text(
        """
class _Writer:
    def __init__(self, fh):
        self._fh = fh
        self._started = False

    def write(self, data):
        if not self._started:
            self._fh.write(b"\\x28\\xb5\\x2f\\xfd")
            self._started = True
        return self._fh.write(data)

    def flush(self):
        return self._fh.flush()

    def close(self):
        return self._fh.close()

    def __enter__(self):
        if not self._started:
            self._fh.write(b"\\x28\\xb5\\x2f\\xfd")
            self._started = True
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _Compressor:
    def stream_writer(self, fh):
        return _Writer(fh)


class _Reader:
    def __init__(self, fh):
        self._fh = fh
        self._skipped = False

    def read(self, size=-1):
        if not self._skipped:
            self._fh.read(4)
            self._skipped = True
        return self._fh.read(size)

    def close(self):
        return self._fh.close()

    def readable(self):
        return True


class _Decompressor:
    def stream_reader(self, fh):
        return _Reader(fh)


ZstdCompressor = _Compressor
ZstdDecompressor = _Decompressor
"""
    )
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
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent.parent / "lpm.py"), "buildpkg", str(script)],
        env=env,
        capture_output=True,
        text=True,
        input="\n",
    )
    assert result.returncode == 0, result.stderr
