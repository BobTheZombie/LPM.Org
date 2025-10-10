#!/usr/bin/env python3
"""Compatibility shim exposing :mod:`src.ui.app` as ``Luminosity``."""

from __future__ import annotations

import importlib.util
import sys


def _tkinter_available() -> bool:
    """Return ``True`` when the Tk runtime is importable."""

    return (
        importlib.util.find_spec("tkinter") is not None
        and importlib.util.find_spec("_tkinter") is not None
    )


if _tkinter_available():
    from src.ui import app as _app

    __all__ = [name for name in _app.__dict__ if not name.startswith("__")]

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

    def main() -> None:
        """Entry point for the Luminosity UI binary."""

        _app.main()


else:
    __all__ = ["main"]

    _MESSAGE = """\
Luminosity requires the Tk runtime, but it is not available in this environment.
Install the ``python3-tk`` (or equivalent) package to enable the graphical
interface, or use the command line client via ``python -m lpm`` instead.
"""

    def main() -> None:
        """Fallback entry point when Tk is missing."""

        sys.stderr.write(_MESSAGE)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
