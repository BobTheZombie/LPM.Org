"""Minimal :mod:`zstandard` compatibility layer for the test environment."""

from __future__ import annotations

from typing import BinaryIO, Optional

_MAGIC = b"\x28\xB5\x2F\xFD"


class _Writer:
    def __init__(self, fh: BinaryIO):
        self._fh = fh
        self._started = False

    def write(self, data: bytes) -> int:
        if not self._started:
            self._fh.write(_MAGIC)
            self._started = True
        self._fh.write(data)
        return len(data)

    def flush(self) -> None:
        if hasattr(self._fh, "flush"):
            self._fh.flush()

    def close(self) -> None:  # pragma: no cover - compatibility shim
        if hasattr(self._fh, "close"):
            self._fh.close()

    def __enter__(self) -> "_Writer":
        self.write(b"")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # pragma: no cover - compatibility shim
        self.flush()
        return None


class _Reader:
    def __init__(self, fh: BinaryIO):
        self._fh = fh
        self._skipped = False

    def read(self, size: int = -1) -> bytes:
        if not self._skipped:
            self._fh.read(len(_MAGIC))
            self._skipped = True
        return self._fh.read(size)

    def readable(self) -> bool:  # pragma: no cover - compatibility shim
        return True

    def close(self) -> None:  # pragma: no cover - compatibility shim
        if hasattr(self._fh, "close"):
            self._fh.close()

    def __iter__(self):  # pragma: no cover - compatibility shim
        while True:
            chunk = self.read(8192)
            if not chunk:
                break
            yield chunk


class ZstdCompressor:
    def stream_writer(self, fh: BinaryIO) -> _Writer:
        return _Writer(fh)

    def compress(self, data: bytes) -> bytes:  # pragma: no cover - compatibility shim
        return _MAGIC + data


class ZstdDecompressor:
    def stream_reader(self, fh: BinaryIO) -> _Reader:
        return _Reader(fh)

    def decompress(self, data: bytes, max_output_size: Optional[int] = None) -> bytes:
        if data.startswith(_MAGIC):
            return data[len(_MAGIC) :]
        return data


__all__ = ["ZstdCompressor", "ZstdDecompressor"]
