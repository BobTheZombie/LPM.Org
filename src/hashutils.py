"""Hash utilities with graceful fallbacks for minimal Python builds.

This module wraps :mod:`hashlib` and provides a SHA-256 constructor that
continues to work even when Python is compiled without the OpenSSL-backed
_hashlib module (e.g. static ``libpython`` builds). In such environments the
standard library may lack working implementations of the common digest
algorithms. We provide a small, pure Python SHA-256 implementation that mimics
Python's hashing interface sufficiently for LPM's needs.
"""

from __future__ import annotations

import struct
from typing import List, Sequence, Union

try:  # pragma: no cover - exercised implicitly
    import hashlib as _stdlib_hashlib
except Exception:  # pragma: no cover - fall back when hashlib import fails
    _stdlib_hashlib = None  # type: ignore[assignment]

BytesLike = Union[bytes, bytearray, memoryview]
Data = Union[str, BytesLike]


def _to_bytes(data: Data) -> bytes:
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, (bytes, bytearray, memoryview)):
        return bytes(data)
    raise TypeError(f"unsupported data type for hashing: {type(data)!r}")


class _PurePythonSHA256:
    """Very small SHA-256 implementation used as a last resort fallback."""

    # Initial hash values and round constants defined by FIPS 180-4.
    _INITIAL_STATE: Sequence[int] = (
        0x6A09E667,
        0xBB67AE85,
        0x3C6EF372,
        0xA54FF53A,
        0x510E527F,
        0x9B05688C,
        0x1F83D9AB,
        0x5BE0CD19,
    )
    _K: Sequence[int] = (
        0x428A2F98, 0x71374491, 0xB5C0FBCF, 0xE9B5DBA5, 0x3956C25B, 0x59F111F1,
        0x923F82A4, 0xAB1C5ED5, 0xD807AA98, 0x12835B01, 0x243185BE, 0x550C7DC3,
        0x72BE5D74, 0x80DEB1FE, 0x9BDC06A7, 0xC19BF174, 0xE49B69C1, 0xEFBE4786,
        0x0FC19DC6, 0x240CA1CC, 0x2DE92C6F, 0x4A7484AA, 0x5CB0A9DC, 0x76F988DA,
        0x983E5152, 0xA831C66D, 0xB00327C8, 0xBF597FC7, 0xC6E00BF3, 0xD5A79147,
        0x06CA6351, 0x14292967, 0x27B70A85, 0x2E1B2138, 0x4D2C6DFC, 0x53380D13,
        0x650A7354, 0x766A0ABB, 0x81C2C92E, 0x92722C85, 0xA2BFE8A1, 0xA81A664B,
        0xC24B8B70, 0xC76C51A3, 0xD192E819, 0xD6990624, 0xF40E3585, 0x106AA070,
        0x19A4C116, 0x1E376C08, 0x2748774C, 0x34B0BCB5, 0x391C0CB3, 0x4ED8AA4A,
        0x5B9CCA4F, 0x682E6FF3, 0x748F82EE, 0x78A5636F, 0x84C87814, 0x8CC70208,
        0x90BEFFFA, 0xA4506CEB, 0xBEF9A3F7, 0xC67178F2,
    )

    __slots__ = ("_buffer", "_count", "_state")

    def __init__(self, data: BytesLike | None = None) -> None:
        self._buffer = b""
        self._count = 0  # number of processed bytes
        self._state = list(self._INITIAL_STATE)
        if data:
            self.update(data)

    @staticmethod
    def _rotr(value: int, shift: int) -> int:
        return ((value >> shift) | (value << (32 - shift))) & 0xFFFFFFFF

    @classmethod
    def _compress(cls, state: Sequence[int], chunk: bytes) -> List[int]:
        assert len(chunk) == 64
        w = list(struct.unpack(">16I", chunk))
        for i in range(16, 64):
            s0 = cls._rotr(w[i - 15], 7) ^ cls._rotr(w[i - 15], 18) ^ (w[i - 15] >> 3)
            s1 = cls._rotr(w[i - 2], 17) ^ cls._rotr(w[i - 2], 19) ^ (w[i - 2] >> 10)
            w.append((w[i - 16] + s0 + w[i - 7] + s1) & 0xFFFFFFFF)

        a, b, c, d, e, f, g, h = state
        for i in range(64):
            s1 = cls._rotr(e, 6) ^ cls._rotr(e, 11) ^ cls._rotr(e, 25)
            ch = (e & f) ^ ((~e) & g)
            temp1 = (h + s1 + ch + cls._K[i] + w[i]) & 0xFFFFFFFF
            s0 = cls._rotr(a, 2) ^ cls._rotr(a, 13) ^ cls._rotr(a, 22)
            maj = (a & b) ^ (a & c) ^ (b & c)
            temp2 = (s0 + maj) & 0xFFFFFFFF

            h, g, f, e, d, c, b, a = (
                g,
                f,
                e,
                (d + temp1) & 0xFFFFFFFF,
                c,
                b,
                a,
                (temp1 + temp2) & 0xFFFFFFFF,
            )

        return [
            (state[0] + a) & 0xFFFFFFFF,
            (state[1] + b) & 0xFFFFFFFF,
            (state[2] + c) & 0xFFFFFFFF,
            (state[3] + d) & 0xFFFFFFFF,
            (state[4] + e) & 0xFFFFFFFF,
            (state[5] + f) & 0xFFFFFFFF,
            (state[6] + g) & 0xFFFFFFFF,
            (state[7] + h) & 0xFFFFFFFF,
        ]

    def update(self, data: BytesLike) -> None:
        chunk = _to_bytes(data)
        self._count += len(chunk)
        chunk = self._buffer + chunk
        for offset in range(0, len(chunk) - len(chunk) % 64, 64):
            block = chunk[offset : offset + 64]
            self._state = self._compress(self._state, block)
        self._buffer = chunk[len(chunk) - len(chunk) % 64 :]

    def _final_state(self) -> List[int]:
        message = self._buffer + b"\x80"
        padding_len = (56 - (len(message) % 64)) % 64
        message += b"\x00" * padding_len
        message += struct.pack(">Q", self._count * 8)

        state = list(self._state)
        for offset in range(0, len(message), 64):
            state = self._compress(state, message[offset : offset + 64])
        return state

    def digest(self) -> bytes:
        state = self._final_state()
        return struct.pack(">8I", *state)

    def hexdigest(self) -> str:
        return self.digest().hex()

    def copy(self) -> "_PurePythonSHA256":
        other = _PurePythonSHA256()
        other._buffer = self._buffer
        other._count = self._count
        other._state = list(self._state)
        return other


def _builtin_sha256_available() -> bool:
    if _stdlib_hashlib is None:
        return False
    try:
        _ = _stdlib_hashlib.sha256  # type: ignore[attr-defined]
    except AttributeError:
        return False
    try:
        _stdlib_hashlib.sha256(b"")
    except ValueError:
        return False
    return True


if _builtin_sha256_available():  # pragma: no cover - relies on system hashlib

    def new_sha256(initial: Data | None = None):
        h = _stdlib_hashlib.sha256()  # type: ignore[union-attr]
        if initial:
            h.update(_to_bytes(initial))
        return h

else:  # pragma: no cover - specific to minimal Python builds

    def new_sha256(initial: Data | None = None):
        h = _PurePythonSHA256()
        if initial:
            h.update(_to_bytes(initial))
        return h


def sha256_hexdigest(data: Data) -> str:
    return new_sha256(data).hexdigest()
