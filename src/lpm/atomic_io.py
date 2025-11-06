from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Union


BytesLike = Union[bytes, bytearray, memoryview]


def _coerce_bytes(data: Union[str, BytesLike], *, encoding: str = "utf-8") -> bytes:
    if isinstance(data, str):
        return data.encode(encoding)
    return bytes(data)


def _sync_directory(path: Path) -> None:
    try:
        dir_fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


@contextmanager
def enforce_umask(mask: int) -> Iterator[None]:
    """Temporarily enforce *mask* as the process umask."""

    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def _current_umask() -> int:
    """Return the process' current umask without permanently altering it."""

    current = os.umask(0)
    os.umask(current)
    return current


def read_bytes(path: Union[str, Path]) -> bytes:
    """Read raw bytes from *path*."""

    return Path(path).read_bytes()


def safe_write(
    path: Union[str, Path],
    data: Union[str, BytesLike],
    *,
    mode: Optional[int] = None,
    owner: Optional[int] = None,
    group: Optional[int] = None,
    encoding: str = "utf-8",
) -> Path:
    """Atomically write *data* to *path*.

    The destination directory is created automatically. If *mode* is provided
    it will be applied to the resulting file (after the active umask is
    honoured). Ownership can be adjusted via *owner* and *group*.
    """

    payload = _coerce_bytes(data, encoding=encoding)
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    prefix = f".{target.name}."
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=prefix, suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())

        mask = _current_umask()
        requested = mode if mode is not None else 0o666
        applied_mode = requested & ~mask
        try:
            os.chmod(tmp_path, applied_mode)
        except OSError:
            pass

        os.replace(tmp_path, target)

        if owner is not None or group is not None:
            uid = owner if owner is not None else -1
            gid = group if group is not None else -1
            try:
                os.chown(target, uid, gid)
            except OSError:
                pass

        try:
            os.chmod(target, applied_mode)
        except OSError:
            pass

        try:
            os.utime(target, None)
        except OSError:
            pass

        _sync_directory(target.parent)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass

    return target


__all__ = [
    "BytesLike",
    "enforce_umask",
    "read_bytes",
    "safe_write",
    "_current_umask",
]
