from pathlib import Path

import pytest

from src.lpm import delta


def test_generate_and_apply_delta(tmp_path: Path):
    base = tmp_path / "base.bin"
    target = tmp_path / "target.bin"
    base.write_bytes(b"A" * 1024 + b"B" * 1024)
    target.write_bytes(b"A" * 1024 + b"C" * 1024)
    patch = tmp_path / "delta.zstpatch"

    meta = delta.generate_delta(base, target, patch, "0.0.0")
    if meta is None:
        pytest.skip("zstd patch support not available")

    out = tmp_path / "out.bin"
    delta.apply_delta(base, patch, out)
    assert out.read_bytes() == target.read_bytes()
    assert meta.delta_size == patch.stat().st_size
