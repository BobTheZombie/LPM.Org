
"""Minimal stub implementation of the :mod:`zstandard` API used in tests."""

from __future__ import annotations

import io
from typing import BinaryIO

_MAGIC = b"\x28\xb5\x2f\xfd"


class ZstdError(Exception):
    """Placeholder exception matching the real library."""


class _StreamWriter:
    def __init__(self, dest: BinaryIO):
        self._dest = dest
        self._closed = False
        self._header_written = False

    def write(self, data: bytes) -> int:
        if self._closed:
            raise ValueError("write to closed stream")
        if not self._header_written:
            self._dest.write(_MAGIC)
            self._header_written = True
        self._dest.write(data)
        return len(data)

    def flush(self) -> None:
        if hasattr(self._dest, "flush"):
            self._dest.flush()

    def close(self) -> None:
        if not self._closed:
            self.flush()
            self._closed = True

    def __enter__(self) -> "_StreamWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
        return None


class _StreamReader(io.RawIOBase):
    def __init__(self, src: BinaryIO):
        super().__init__()
        self._src = src
        self._closed = False
        self._header_checked = False

    def readable(self) -> bool:  # pragma: no cover - simple shim
        return True

    def read(self, size: int = -1) -> bytes:
        if self._closed:
            return b""
        if not self._header_checked:
            header = self._src.read(len(_MAGIC))
            if header and header != _MAGIC:
                raise ZstdError("invalid zstd magic header")
            self._header_checked = True
        data = self._src.read(size)
        if data == b"":
            self.close()
        return data

    def close(self) -> None:
        if not self._closed:
            if hasattr(self._src, "close"):
                self._src.close()
            self._closed = True

    def __enter__(self) -> "_StreamReader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
        return None


class ZstdCompressor:
    def stream_writer(self, dest: BinaryIO) -> _StreamWriter:
        return _StreamWriter(dest)

    def compress(self, data: bytes) -> bytes:  # pragma: no cover - compatibility helper
        return _MAGIC + data


class ZstdDecompressor:
    def stream_reader(self, src: BinaryIO) -> _StreamReader:
        return _StreamReader(src)

    def decompress(self, data: bytes) -> bytes:
        if not data:
            return data
        if data.startswith(_MAGIC):
            return data[len(_MAGIC) :]
        raise ZstdError("invalid zstd magic header")


__all__ = [
    "ZstdCompressor",
    "ZstdDecompressor",
    "ZstdError",
]
