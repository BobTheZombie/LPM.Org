from __future__ import annotations

import tarfile
from pathlib import Path

from lpm import app as lpm


def test_create_snapshot_keeps_atomic_output_open(monkeypatch, tmp_path):
    source = tmp_path / "payload.txt"
    source.write_text("snapshot payload", encoding="utf-8")
    snapshot_dir = tmp_path / "snapshots"

    class Connection:
        def execute(self, *_args):
            return None

        def commit(self):
            return None

        def close(self):
            return None

    monkeypatch.setattr(lpm, "SNAPSHOT_DIR", snapshot_dir)
    monkeypatch.setattr(lpm, "db", lambda: Connection())
    monkeypatch.setattr(lpm, "prune_snapshots", lambda _limit: None)
    monkeypatch.setattr(
        lpm,
        "operation_phase",
        lambda **_kwargs: lpm.contextlib.nullcontext(),
    )

    archive = Path(lpm.create_snapshot("install-demo", [source]))

    assert archive.is_file()
    with archive.open("rb") as compressed:
        with lpm.zstd.ZstdDecompressor().stream_reader(compressed) as reader:
            with tarfile.open(fileobj=reader, mode="r|") as tar:
                member = next(item for item in tar if item.name == source.as_posix().lstrip("/"))
                extracted = tar.extractfile(member)
                assert extracted is not None
                assert extracted.read() == b"snapshot payload"
