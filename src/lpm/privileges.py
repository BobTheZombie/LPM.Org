"""Privilege-handling stubs.

This module previously handled dynamic privilege escalation and dropping. The
system now assumes callers execute LPM directly with ``sudo`` (or as root), so
the helpers here simply provide no-op contexts for compatibility.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator


@contextmanager
def privileged_section() -> Iterator[None]:
    """Provide a no-op privileged section for legacy callers."""

    yield


__all__ = [
    "privileged_section",
]

