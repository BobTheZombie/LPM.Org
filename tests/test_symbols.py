import os
import sys
import subprocess
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from lpm import collect_manifest


def test_collect_manifest_captures_exported_symbols(tmp_path):
    src = tmp_path / "foo.c"
    src.write_text("int foo() { return 42; }\n")
    so = tmp_path / "libfoo.so"
    subprocess.run(["gcc", "-shared", "-fPIC", str(src), "-o", str(so)], check=True)
    mani = collect_manifest(tmp_path)
    entry = next(e for e in mani if e["path"] == "/libfoo.so")
    assert "symbols" in entry
    assert "foo" in entry["symbols"]
