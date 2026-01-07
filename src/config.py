"""Compatibility shim for legacy config imports."""
from lpm import config as _config
import sys as _sys

_sys.modules[__name__] = _config
