"""Lightweight stand-in for the Hypothesis library used in tests.

This module intentionally implements only the tiny subset of the public
Hypothesis API that is exercised by our property based tests.  It is not a
full replacement for Hypothesis and should not be relied upon for general
use.  The real project is far more feature rich; here we provide just enough
structure to allow the tests to run in environments where Hypothesis is not
installed.
"""

from . import strategies
from .core import Settings, given, settings

__all__ = ["Settings", "given", "settings", "strategies"]
