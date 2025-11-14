from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional, Union

from importlib import import_module

try:
    import lpm.config as config
except ModuleNotFoundError:  # pragma: no cover - fallback for bundled entry points
    parts = [p for p in (__package__ or "").split(".") if p]
    if "lpm" in parts:
        idx = parts.index("lpm") + 1
        candidates = [".".join(parts[:idx])]
    elif parts:
        candidates = [parts[0]]
    else:
        candidates = []
    candidates.append("lpm")
    config = None
    for base in dict.fromkeys(candidates):
        try:
            config = import_module(f"{base}.config")
            break
        except ModuleNotFoundError:
            continue
    if config is None:
        raise


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


def _ensure_parents(path: Path) -> list[Path]:
    """Ensure parent directories for *path* exist and return any created ones."""

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


def _apply_metadata(
    tmp_path: Path,
    target: Path,
    *,
    mode: Optional[int],
    owner: Optional[int],
    group: Optional[int],
    extra_sync: Iterable[Path] = (),
) -> None:
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

    sync_targets = {target.parent}
    for directory in extra_sync:
        sync_targets.add(directory)
        parent = directory.parent
        if parent != directory:
            sync_targets.add(parent)
    for directory in sync_targets:
        _sync_directory(directory)


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
    created_dirs = _ensure_parents(target)

    prefix = f".{target.name}."
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=prefix, suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        fh = os.fdopen(fd, "wb")
        try:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fh.close()

        _apply_metadata(
            tmp_path,
            target,
            mode=mode,
            owner=owner,
            group=group,
            extra_sync=created_dirs,
        )
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass

    return target


@contextmanager
def atomic_replace(
    path: Union[str, Path],
    *,
    mode: Optional[int] = None,
    owner: Optional[int] = None,
    group: Optional[int] = None,
    open_mode: str = "wb",
    encoding: str = "utf-8",
) -> Iterator[object]:
    """Yield a writable handle that atomically replaces *path* on success."""

    target = Path(path).resolve()
    created_dirs = _ensure_parents(target)

    prefix = f".{target.name}."
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=prefix, suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        open_kwargs = {}
        if "b" not in open_mode:
            open_kwargs["encoding"] = encoding
        handle = os.fdopen(fd, open_mode, **open_kwargs)
        try:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            handle.close()

        _apply_metadata(
            tmp_path,
            target,
            mode=mode,
            owner=owner,
            group=group,
            extra_sync=created_dirs,
        )
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise
    else:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


__all__ = [
    "BytesLike",
    "enforce_umask",
    "read_bytes",
    "safe_write",
    "atomic_replace",
    "_current_umask",
    "atomic_write_bytes",
    "atomic_write_text",
    "atomic_write_json",
]


@contextmanager
def _config_umask() -> Iterator[None]:
    previous = os.umask(config.UMASK)
    try:
        yield
    finally:
        os.umask(previous)


def atomic_write_bytes(path: Union[str, Path], data: Union[str, BytesLike]) -> None:
    with _config_umask():
        safe_write(path, data, mode=0o666)


def atomic_write_text(
    path: Union[str, Path],
    text: str,
    *,
    encoding: str = "utf-8",
) -> None:
    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: Union[str, Path], obj: object) -> None:
    payload = json.dumps(obj, indent=2, sort_keys=True)
    atomic_write_text(path, payload)
