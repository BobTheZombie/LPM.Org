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

    from . import config

    previous = os.umask(config.UMASK)
    try:
        yield
    finally:
        os.umask(previous)


def _target_permissions() -> int:
    from . import config

    return 0o666 & ~config.UMASK


def _write_bytes(path: Path, data: bytes) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    prefix = f".{path.name}."

    with _enforce_umask():
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=prefix, suffix=".tmp")

    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())

        os.chmod(tmp_path, _target_permissions())
        os.replace(tmp_path, path)
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
