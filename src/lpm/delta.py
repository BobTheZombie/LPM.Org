"""Helpers for zstd-based delta package generation and application."""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
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


@lru_cache(maxsize=512)
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

    version = zstd_version()
    if not version_at_least(version, minimum_version):
        return None
    if not ZSTD_BIN:
        return None
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ZSTD_BIN, f"--patch-from={str(base)}", str(target), "-o", str(output)]
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError:
        if output.exists():
            output.unlink()
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
    cmd = [ZSTD_BIN, f"--patch-from={str(base)}", str(patch), "-d", "-o", str(output)]
    subprocess.check_call(cmd)


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

