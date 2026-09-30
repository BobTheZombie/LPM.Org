from pathlib import Path
import subprocess

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


def test_file_sha256_is_not_stale_when_path_is_replaced(tmp_path: Path):
    package = tmp_path / "package.zst"
    package.write_bytes(b"old package")
    old_digest = delta.file_sha256(package)
    package.write_bytes(b"new package")
    assert delta.file_sha256(package) != old_digest


def test_failed_delta_generation_preserves_existing_output(tmp_path: Path, monkeypatch):
    base = tmp_path / "base.zst"
    target = tmp_path / "target.zst"
    output = tmp_path / "delta.zstpatch"
    base.write_bytes(b"base")
    target.write_bytes(b"target")
    output.write_bytes(b"published delta")

    monkeypatch.setattr(delta, "ZSTD_BIN", "/usr/bin/zstd")
    monkeypatch.setattr(delta, "zstd_version", lambda: (99, 0, 0))
    monkeypatch.setattr(
        delta.subprocess,
        "check_call",
        lambda _cmd: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "zstd")),
    )

    assert delta.generate_delta(base, target, output, "1.5.5") is None
    assert output.read_bytes() == b"published delta"


def test_failed_delta_application_preserves_existing_output(tmp_path: Path, monkeypatch):
    base = tmp_path / "base.zst"
    patch = tmp_path / "delta.zstpatch"
    output = tmp_path / "package.zst"
    base.write_bytes(b"base")
    patch.write_bytes(b"patch")
    output.write_bytes(b"installed package")

    monkeypatch.setattr(delta, "ZSTD_BIN", "/usr/bin/zstd")
    monkeypatch.setattr(
        delta.subprocess,
        "check_call",
        lambda _cmd: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "zstd")),
    )

    with pytest.raises(subprocess.CalledProcessError):
        delta.apply_delta(base, patch, output)
    assert output.read_bytes() == b"installed package"
