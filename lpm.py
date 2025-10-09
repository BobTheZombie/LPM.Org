#!/usr/bin/env python3
"""Compatibility shim exposing the :mod:`src.lpm.app` module as ``lpm``."""

from src.lpm import app as _app

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

__all__ = [name for name in _app.__dict__ if not name.startswith("__")]


def main(argv=None):
    """Entry point for ``python -m lpm`` or direct script execution."""

    return _app.main(argv)


if __name__ == "__main__":
    _app.main()
