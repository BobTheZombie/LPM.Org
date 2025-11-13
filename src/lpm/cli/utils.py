"""Utility helpers shared by the CLI commands."""

from __future__ import annotations

import os


def is_root() -> bool:
    """Return ``True`` when the current process is running as ``root``."""

    geteuid = getattr(os, "geteuid", None)
    if not callable(geteuid):
        return False
    try:
        return geteuid() == 0
    except OSError:
        return False


__all__ = ["is_root"]

