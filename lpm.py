#!/usr/bin/env python3
"""Compatibility shim exposing :mod:`src.lpm.app` as ``lpm``."""

import importlib
import os
import sys

_PRESERVED_ATTRS = ("ResolutionError",)

if "src.lpm.app" in sys.modules:
    previous = sys.modules["src.lpm.app"]
    preserved = {
        name: getattr(previous, name)
        for name in _PRESERVED_ATTRS
        if hasattr(previous, name)
    }
    if os.environ.get("LPM_SHIM_DISABLE_RELOAD") == "1":
        _app = previous
    else:
        _app = importlib.reload(previous)  # type: ignore[assignment]
        for name, value in preserved.items():
            setattr(_app, name, value)
else:
    from src.lpm import app as _app

# Populate the shim namespace for direct execution while keeping the true module alive.
globals().update(
    {
        name: value
        for name, value in _app.__dict__.items()
        if name not in {
            "__name__",
            "__package__",
            "__loader__",
            "__spec__",
            "__file__",
        }
    }
)

# Ensure consumers interact with the real module so monkeypatching works.
sys.modules[__name__] = _app
setattr(_app, "__file__", __file__)


def main(argv=None):
    """Entry point for ``python -m lpm`` or direct script execution."""

    return _app.main(argv)


if __name__ == "__main__":
    _app.main()
