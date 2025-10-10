#!/usr/bin/env python3
"""Compatibility shim exposing :mod:`src.ui.app` as ``Luminosity``."""

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


if __name__ == "__main__":
    _app.main()
