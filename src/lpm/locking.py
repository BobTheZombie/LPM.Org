from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import fcntl

from . import config


class TransactionLockError(RuntimeError):
    """Raised when a concurrent transaction is already in progress."""

    def __init__(self, lock_path: Path, holder_pid: Optional[int]) -> None:
        self.lock_path = lock_path
        self.holder_pid = holder_pid
        message = "another transaction is running"
        if holder_pid is not None:
            message = f"{message} (pid {holder_pid})"
        super().__init__(message)


@dataclass
class _LockHandle:
    path: Path
    fd: int

    def release(self) -> None:
        try:
            os.ftruncate(self.fd, 0)
            os.fsync(self.fd)
        finally:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)


def _read_pid(fd: int) -> Optional[int]:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError:
        return None
    try:
        data = os.read(fd, 32)
    except OSError:
        return None
    if not data:
        return None
    try:
        return int(data.decode("utf-8").strip())
    except (ValueError, UnicodeDecodeError):
        return None


def _acquire(lock_path: Path) -> _LockHandle:
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    flags = os.O_RDWR | os.O_CREAT
    fd = os.open(lock_path, flags, 0o666 & ~config.UMASK)
    os.chmod(lock_path, 0o666 & ~config.UMASK)

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = _read_pid(fd)
        os.close(fd)
        raise TransactionLockError(lock_path, holder)

    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode("ascii"))
    os.fsync(fd)
    return _LockHandle(lock_path, fd)


class GlobalTransactionLock:
    """Context manager providing a process-wide transaction lock."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path or config.LOCK_PATH
        self._handle: Optional[_LockHandle] = None

    def __enter__(self) -> "GlobalTransactionLock":
        self._handle = _acquire(self.path)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is not None:
            self._handle.release()
            self._handle = None


def global_transaction_lock(path: Optional[Path] = None) -> GlobalTransactionLock:
    return GlobalTransactionLock(path)


__all__ = [
    "GlobalTransactionLock",
    "TransactionLockError",
    "global_transaction_lock",
]
