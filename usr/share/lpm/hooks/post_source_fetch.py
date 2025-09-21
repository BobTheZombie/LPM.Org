#!/usr/bin/env python3
"""Hook that unpacks the primary source archive after fetching."""

from __future__ import annotations

import os
import subprocess
import tarfile
from pathlib import Path
from typing import Iterable, Optional


_SIGNATURE_SUFFIXES = (".sig", ".asc")
_EXTRA_COMPRESSED_EXTENSIONS = (".tar.zst", ".tar.lz4", ".tar.lzo")


def _is_signature(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(suffix) for suffix in _SIGNATURE_SUFFIXES)


def _is_extractable(path: Path) -> bool:
    if not path.is_file():
        return False
    if _is_signature(path):
        return False
    if tarfile.is_tarfile(path):
        return True
    name = path.name.lower()
    return any(name.endswith(ext) for ext in _EXTRA_COMPRESSED_EXTENSIONS)


def _select_tarball(paths: Iterable[Path]) -> Optional[Path]:
    for candidate in paths:
        if _is_extractable(candidate):
            return candidate
    return None


def _iter_entries(srcroot: Path, entries_env: str) -> Iterable[Path]:
    for raw in entries_env.splitlines():
        entry = raw.strip()
        if not entry:
            continue
        candidate = Path(entry)
        if not candidate.is_absolute():
            candidate = srcroot / candidate
        yield candidate


def main() -> int:
    srcroot_env = os.environ.get("LPM_SRCROOT")
    if not srcroot_env:
        return 0

    srcroot = Path(srcroot_env)
    entries_env = os.environ.get("LPM_SOURCE_ENTRIES", "")
    archive = _select_tarball(_iter_entries(srcroot, entries_env))

    if archive is None:
        archive = _select_tarball(sorted(srcroot.rglob("*")))

    if archive is None:
        return 0

    name = os.environ.get("LPM_NAME")
    version = os.environ.get("LPM_VERSION")
    if not name or not version:
        return 0

    target_dir = srcroot / f"{name}-{version}"
    target_dir.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.run(
            [
                "tar",
                "--strip-components=1",
                "-xaf",
                str(archive),
                "-C",
                str(target_dir),
            ],
            check=True,
        )
    except FileNotFoundError:
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

