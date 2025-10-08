"""Fallback stub for the :mod:`zstandard` package used in tests.

The real ``zstandard`` package is not available in the execution environment
used for the kata.  The production code only relies on a very small subset of
its streaming API, so we provide a minimal passthrough implementation that
behaves like an identity transform.  This keeps the rest of the codebase
agnostic about whether the optional dependency is installed while allowing the
existing tests to exercise the packaging logic.
"""
from __future__ import annotations

from typing import BinaryIO, Optional

_MAGIC = b"\x28\xb5\x2f\xfd"


class ZstdError(Exception):
    """Mirror the real zstandard exception hierarchy."""


class _StreamBase:
    def __init__(self, fileobj: BinaryIO):
        self._fileobj = fileobj
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if hasattr(self._fileobj, "close"):
                self._fileobj.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # Propagate exceptions to match the behaviour of the real context
        # managers exposed by :mod:`zstandard`.
        return False


class _PassthroughWriter(_StreamBase):
    def __init__(self, fileobj: BinaryIO):
        super().__init__(fileobj)
        self._header_written = False

    def write(self, data: bytes | bytearray | memoryview) -> int:
        if isinstance(data, memoryview):
            data = data.tobytes()
        if not self._header_written:
            self._fileobj.write(_MAGIC)
            self._header_written = True
        written = self._fileobj.write(data)
        if written is None:
            written = len(data)
        return written

    def flush(self) -> None:
        if hasattr(self._fileobj, "flush"):
            self._fileobj.flush()

    def writable(self) -> bool:  # pragma: no cover - convenience API
        return True

    def close(self) -> None:
        if not self._header_written:
            self._fileobj.write(_MAGIC)
            self._header_written = True
        super().close()


class _PassthroughReader(_StreamBase):
    def __init__(self, fileobj: BinaryIO):
        super().__init__(fileobj)
        magic = self._fileobj.read(len(_MAGIC))
        if magic != _MAGIC:
            raise ZstdError("invalid zstd header")

    def read(self, size: Optional[int] = -1) -> bytes:
        data = self._fileobj.read(size)
        if data is None:
            return b""
        return data

    def readable(self) -> bool:  # pragma: no cover - convenience API
        return True


class ZstdCompressor:
    """Identity compressor used when the optional dependency is missing."""

    def __init__(self, *_, **__):
        pass

    def stream_writer(self, fileobj: BinaryIO) -> _PassthroughWriter:
        return _PassthroughWriter(fileobj)

    def compress(self, data: bytes) -> bytes:
        if isinstance(data, memoryview):
            data = data.tobytes()
        return _MAGIC + bytes(data)


class ZstdDecompressor:
    """Identity decompressor used when the optional dependency is missing."""

    def __init__(self, *_, **__):
        pass

    def stream_reader(self, fileobj: BinaryIO) -> _PassthroughReader:
        return _PassthroughReader(fileobj)

    def decompress(self, data: bytes) -> bytes:
        if isinstance(data, memoryview):
            data = data.tobytes()
        if not data.startswith(_MAGIC):
            raise ZstdError("invalid zstd header")
        return bytes(data[len(_MAGIC):])


__all__ = [
    "ZstdCompressor",
    "ZstdDecompressor",
    "ZstdError",
]
