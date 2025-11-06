from __future__ import annotations

import json
from pathlib import Path

import pytest

from lpm.fs_ops import (
    journal_append,
    operation_phase,
    write_db_bytes,
    write_db_json,
    write_manifest_file,
)


def test_write_db_json_umask(tmp_path: Path):
    target = tmp_path / "index.json"
    with operation_phase(privileged=True):
        write_db_json(target, {"b": 2, "a": 1})
    contents = target.read_text("utf-8").strip()
    assert contents == json.dumps({"a": 1, "b": 2}, ensure_ascii=False, sort_keys=True, indent=2)
    assert (target.stat().st_mode & 0o777) == 0o644


def test_write_db_bytes(tmp_path: Path):
    target = tmp_path / "index.bin"
    with operation_phase(privileged=True):
        write_db_bytes(target, b"\x00\xff")
    assert target.read_bytes() == b"\x00\xff"
    assert (target.stat().st_mode & 0o777) == 0o644


def test_manifest_write_text_and_bytes(tmp_path: Path):
    root = tmp_path / "root"
    with operation_phase(privileged=True):
        text_path = write_manifest_file(root, "etc/foo.conf", "x=1\n", mode=0o644, is_text=True)
        bin_path = write_manifest_file(root, "bin/tool", b"\x7fELF...", mode=0o755)
    assert text_path.read_text() == "x=1\n"
    assert (text_path.stat().st_mode & 0o777) == 0o644
    assert bin_path.read_bytes().startswith(b"\x7fELF")
    assert (bin_path.stat().st_mode & 0o777) == 0o755
    etc_dir = (root / "etc").resolve()
    bin_dir = (root / "bin").resolve()
    assert (etc_dir.stat().st_mode & 0o777) == 0o755
    assert (bin_dir.stat().st_mode & 0o777) == 0o755


def test_journal_ldjson(tmp_path: Path):
    journal = tmp_path / "journal.ldjson"
    with operation_phase(privileged=True):
        journal_append(journal, {"op": "install", "pkg": "foo", "ver": "1"})
        journal_append(journal, {"op": "install", "pkg": "bar", "ver": "2"})
    lines = journal.read_text("utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert first["pkg"] == "foo"
    assert second["pkg"] == "bar"
    assert (journal.stat().st_mode & 0o777) == 0o640


@pytest.mark.parametrize("privileged", [True, False])
def test_operation_phase_context(tmp_path: Path, privileged: bool):
    target = tmp_path / "file"
    with operation_phase(privileged=privileged):
        from lpm.atomic_io import _current_umask, safe_write

        current = _current_umask()
        safe_write(target, b"x", mode=0o666)
    resulting_mode = target.stat().st_mode & 0o777
    if privileged:
        assert resulting_mode == 0o644
    else:
        assert resulting_mode == (0o666 & ~current)
