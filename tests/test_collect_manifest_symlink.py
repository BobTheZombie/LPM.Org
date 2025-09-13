import os
import sys
import hashlib

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from lpm import collect_manifest


def test_collect_manifest_handles_broken_symlink(tmp_path):
    link = tmp_path / "broken"
    link.symlink_to("missing")
    mani = collect_manifest(tmp_path)
    entry = next(e for e in mani if e["path"] == "/broken")
    assert entry["link"] == "missing"
    assert entry["sha256"] == hashlib.sha256(b"missing").hexdigest()
    assert entry["size"] == os.lstat(link).st_size
    assert "symbols" not in entry

