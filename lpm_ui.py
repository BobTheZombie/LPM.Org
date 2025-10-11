#!/usr/bin/env python3
"""Entry point for the LPM graphical user interface."""

from __future__ import annotations

import importlib.util
import sys


def _tkinter_available() -> bool:
    """Return ``True`` when the Tk runtime is importable."""

    return (
        importlib.util.find_spec("tkinter") is not None
        and importlib.util.find_spec("_tkinter") is not None
    )


def main() -> None:
    """Launch the graphical frontend."""

    if not _tkinter_available():
        sys.stderr.write(
            "The LPM graphical interface requires the Tk runtime.\n"
            "Install the `python3-tk` (or equivalent) package to enable it, "
            "or use the command line client via `python -m lpm`.\n"
        )
        raise SystemExit(1)

    from src.ui.app import main as app_main

    app_main()


if __name__ == "__main__":
    main()
