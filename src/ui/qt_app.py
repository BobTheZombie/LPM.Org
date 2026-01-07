"""Qt-based graphical shell for LPM.

This module provides a PySide6 user interface that mirrors the behaviour
of the legacy Tkinter front-end while offering a more contemporary and
visually appealing experience.  It communicates with the LPM backend via
``ui.backend.LPMBackend`` and executes CLI operations in background
threads so the interface remains responsive.
"""

from __future__ import annotations

import importlib.util
import sys
from typing import Callable


def _qt_available() -> bool:
    """Return ``True`` when the PySide6 runtime is importable."""

    return importlib.util.find_spec("PySide6") is not None


def _missing_qt() -> Callable[[], None]:
    message = (
        "The LPM graphical interface requires PySide6.\n"
        "Install the dependencies listed in requirements-ui.txt, "
        "or use the command line client via `python -m lpm`.\n"
    )

    def _exit() -> None:
        sys.stderr.write(message)
        raise SystemExit(1)

    return _exit


if _qt_available():
    from .qt_app_impl import main
else:
    main = _missing_qt()

__all__ = ["main"]
