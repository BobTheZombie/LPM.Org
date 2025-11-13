"""Command line entry points for :mod:`lpm`.

This package exposes a modular command line interface where each command lives
in its own module.  The :func:`main` function defined here is imported by the
``lpm`` console script.
"""

from __future__ import annotations

from .main import main

__all__ = ["main"]

