from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Iterator


@contextlib.contextmanager
def _enforce_umask() -> Iterator[None]:
    """Temporarily apply the configured umask for the duration of an operation."""

    from lpm import config

    previous = os.umask(config.UMASK)
    try:
        yield
    finally:
        os.umask(previous)


def _target_permissions() -> int:
    from lpm import config

    return 0o666 & ~config.UMASK


def _sync_directory(path: Path) -> None:
    """Best-effort fsync of *path* when it refers to a directory."""

    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        dir_fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


def _ensure_parents(path: Path) -> list[Path]:
    """Create parent directories for *path* and return the ones created."""

    created: list[Path] = []
    parent = path.parent
    missing: list[Path] = []
    while True:
        if parent.exists():
            break
        missing.append(parent)
        parent = parent.parent
    for directory in reversed(missing):
        try:
            directory.mkdir()
            created.append(directory)
        except FileExistsError:
            continue
    return created


def _write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    created_dirs: list[Path] = []

    prefix = f".{path.name}."

    with _enforce_umask():
        created_dirs = _ensure_parents(path)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=prefix, suffix=".tmp")

    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())

        os.chmod(tmp_path, _target_permissions())
        os.replace(tmp_path, path)

        sync_targets = {path.parent}
        for directory in created_dirs:
            sync_targets.add(directory)
            parent = directory.parent
            if parent != directory:
                sync_targets.add(parent)
        for directory in sync_targets:
            _sync_directory(directory)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write raw bytes to *path* while respecting ``config.UMASK``."""

    _write_bytes(path, data)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically write *text* to *path* using ``encoding``."""

    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: Path, obj) -> None:
    """Atomically serialize *obj* as formatted JSON to *path*."""

    data = json.dumps(obj, indent=2, sort_keys=True).encode("utf-8")
    atomic_write_bytes(path, data)


__all__ = [
    "atomic_write_bytes",
    "atomic_write_text",
    "atomic_write_json",
]
