"""Helpers for zstd-based delta package generation and application."""
from __future__ import annotations

import re
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Optional, Sequence, Tuple

ZSTD_BIN = shutil.which("zstd")


@dataclass
class DeltaMeta:
    """Metadata describing a generated delta artifact."""

    algorithm: str
    base_version: str
    base_sha256: str
    delta_sha256: str
    delta_size: int
    min_tool: str


def _hash(path: Path) -> str:
    h = sha256()
    with path.open("rb", buffering=1024 * 1024) as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def file_sha256(path: Path) -> str:
    """Return the SHA-256 for *path* with a small read buffer."""

    return _hash(Path(path))


def zstd_version() -> Optional[Tuple[int, int, int]]:
    if not ZSTD_BIN:
        return None
    try:
        out = subprocess.check_output([ZSTD_BIN, "--version"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return None
    match = re.search(r"v(\d+)\.(\d+)\.(\d+)", out)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def version_at_least(current: Optional[Tuple[int, int, int]], minimum: str) -> bool:
    if current is None:
        return False
    want = tuple(int(part) for part in minimum.split("."))
    return current >= want


def delta_relpath(name: str, version: str, arch: str, base_version: str) -> Path:
    return Path("deltas") / name / version / arch / f"{base_version}.zstpatch"


def generate_delta(base: Path, target: Path, output: Path, minimum_version: str) -> Optional[DeltaMeta]:
    """Generate a delta between *base* and *target* using zstd."""

    base = Path(base)
    target = Path(target)
    output = Path(output)
    if not base.is_file() or not target.is_file():
        raise FileNotFoundError("delta base and target must both be regular files")
    version = zstd_version()
    if not version_at_least(version, minimum_version):
        return None
    if not ZSTD_BIN:
        return None
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    os.close(fd)
    tmp_output = Path(tmp_name)
    cmd = [ZSTD_BIN, f"--patch-from={str(base)}", str(target), "-f", "-o", str(tmp_output)]
    try:
        subprocess.check_call(cmd)
        # A delta larger than the target package wastes bandwidth and storage.
        if tmp_output.stat().st_size >= target.stat().st_size:
            tmp_output.unlink()
            return None
        os.replace(tmp_output, output)
    except (OSError, subprocess.CalledProcessError):
        if tmp_output.exists():
            tmp_output.unlink()
        return None
    return DeltaMeta(
        algorithm="zstd-patch",
        base_version="",
        base_sha256=file_sha256(base),
        delta_sha256=file_sha256(output),
        delta_size=output.stat().st_size,
        min_tool=f"zstd>={minimum_version}",
    )


def apply_delta(base: Path, patch: Path, output: Path) -> None:
    if not ZSTD_BIN:
        raise RuntimeError("zstd binary not available for delta application")
    base = Path(base)
    patch = Path(patch)
    output = Path(output)
    if not base.is_file() or not patch.is_file():
        raise FileNotFoundError("delta base and patch must both be regular files")
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    os.close(fd)
    tmp_output = Path(tmp_name)
    cmd = [ZSTD_BIN, f"--patch-from={str(base)}", str(patch), "-d", "-f", "-o", str(tmp_output)]
    try:
        subprocess.check_call(cmd)
        os.replace(tmp_output, output)
    except Exception:
        if tmp_output.exists():
            tmp_output.unlink()
        raise


def find_cached_by_sha(cache_dirs: Sequence[Path], digest: str) -> Optional[Path]:
    """Return a cached file matching *digest* if present."""

    for directory in cache_dirs:
        if not directory.exists():
            continue
        for entry in directory.iterdir():
            try:
                if not entry.is_file():
                    continue
                if entry.suffix != ".zst" and not entry.name.endswith(".tar.zst"):
                    continue
                if file_sha256(entry) == digest:
                    return entry
            except Exception:
                continue
    return None
