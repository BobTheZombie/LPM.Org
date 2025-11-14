"""Compatibility wrapper for legacy ``src`` imports.

The project now exposes its public API from :mod:`lpm`. Importing from the
``src`` package still works for the time being, but it is deprecated and will
be removed in a future release.
"""

from __future__ import annotations

import warnings
from importlib import import_module
from typing import Any

_LPM = import_module("lpm")

warnings.warn(
    "Importing from 'src' is deprecated; use the 'lpm' package instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = getattr(_LPM, "__all__", ())


def __getattr__(name: str) -> Any:  # pragma: no cover - passthrough wrapper
    try:
        return getattr(_LPM, name)
    except AttributeError as exc:  # pragma: no cover - mirror normal behaviour
        raise AttributeError(name) from exc


def __dir__() -> list[str]:  # pragma: no cover - compatibility helper
    return sorted(set(dir(_LPM)))
