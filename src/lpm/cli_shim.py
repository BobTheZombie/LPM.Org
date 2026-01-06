"""Compatibility launcher for the :mod:`lpm` CLI package.

This module mirrors the behaviour of the historical ``lpm.py`` shim while
living inside the installable package namespace.  Keeping the logic here lets
``console_scripts`` entry points import a stable module regardless of whether
LPM is executed from a source checkout or an installed wheel.
"""

from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType
from typing import Iterable

_PRESERVED_ATTRS: Iterable[str] = ("ResolutionError",)
_MODULE_PREFIXES = ("lpm", "src.lpm")


def _import_with_fallback(module: str) -> ModuleType:
    """Import ``module`` trying standard and source-layout prefixes."""

    last_exc: Exception | None = None
    for prefix in _MODULE_PREFIXES:
        try:
            return importlib.import_module(f"{prefix}.{module}")
        except ModuleNotFoundError as exc:
            last_exc = exc
    raise last_exc  # type: ignore[misc]


def _reload_app() -> ModuleType:
    """Import or reload the primary :mod:`lpm.app` module."""

    preserved: dict[str, object] = {}
    if __name__ in sys.modules:
        previous = sys.modules[__name__]
        preserved = {
            name: getattr(previous, name)
            for name in _PRESERVED_ATTRS
            if hasattr(previous, name)
        }
        if os.environ.get("LPM_SHIM_DISABLE_RELOAD") == "1":
            return previous

    module = _import_with_fallback("app")
    if os.environ.get("LPM_SHIM_DISABLE_RELOAD") != "1":
        module = importlib.reload(module)  # type: ignore[assignment]
        for name, value in preserved.items():
            setattr(module, name, value)
    return module


def _expose_helpers(app: ModuleType) -> None:
    """Make helper modules available on the :mod:`lpm` namespace."""

    _fs_ops = _import_with_fallback("fs_ops")
    _atomic_io = _import_with_fallback("atomic_io")
    _privileges = _import_with_fallback("privileges")
    setattr(app, "fs_ops", _fs_ops)
    setattr(app, "atomic_io", _atomic_io)
    setattr(app, "privileges", _privileges)
    sys.modules.setdefault("lpm.fs_ops", _fs_ops)
    sys.modules.setdefault("lpm.atomic_io", _atomic_io)
    sys.modules.setdefault("lpm.privileges", _privileges)


_app = _reload_app()
_expose_helpers(_app)

# Populate the shim namespace for direct execution while keeping the true
# module alive.  This mirrors the original top-level shim so ``python lpm.py``
# behaves the same way as ``python -m lpm``.
globals().update(
    {
        name: value
        for name, value in _app.__dict__.items()
        if name
        not in {
            "__name__",
            "__package__",
            "__loader__",
            "__spec__",
            "__file__",
        }
    }
)
sys.modules.setdefault(__name__, _app)
setattr(_app, "__file__", __file__)


def main(argv=None):
    """Entry point for ``python -m lpm`` or direct script execution."""

    return _app.main(argv)


if __name__ == "__main__":
    _app.main()
