#!/usr/bin/env python3
"""Compatibility shim exposing :mod:`src.lpm.cli_shim` as ``lpm``."""

from __future__ import annotations

import importlib
import sys

_shim = importlib.import_module("src.lpm.cli_shim")

# Mirror the original behaviour where importing ``lpm`` provided access to the
# underlying :mod:`lpm` package while keeping the shim module alive.
globals().update(_shim.__dict__)
sys.modules[__name__] = _shim


def main(argv=None):
    """Entry point for ``python -m lpm`` or direct script execution."""

    return _shim.main(argv)


if __name__ == "__main__":
    _shim.main()
