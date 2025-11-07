#!/usr/bin/env python3
"""Entry point for the LPM graphical user interface."""

from __future__ import annotations

import importlib.util
import sys


def _qt_available() -> bool:
    """Return ``True`` when the PySide6 runtime is importable."""

    return importlib.util.find_spec("PySide6") is not None


def main() -> None:
    """Launch the graphical frontend."""

    if not _qt_available():
        sys.stderr.write(
            "The LPM graphical interface requires PySide6.\n"
            "Install the dependencies listed in requirements-ui.txt, "
            "or use the command line client via `python -m lpm`.\n"
        )
        raise SystemExit(1)

    from src.ui.qt_app import main as app_main

    app_main()


if __name__ == "__main__":
    main()
