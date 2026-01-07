"""Convenience exports for LPM library components."""

from importlib import import_module
from typing import Any

from .lpm.resolver import CDCLSolver, CNF, Implication, SATResult
from .lpm.hooks import (
    Hook,
    HookAction,
    HookError,
    HookTransactionManager,
    HookTrigger,
    load_hooks,
)

__all__ = [
    "CNF",
    "SATResult",
    "Implication",
    "CDCLSolver",
    "main",
    "ResolutionError",
    "get_runtime_metadata",
    "Hook",
    "HookAction",
    "HookTransactionManager",
    "HookTrigger",
    "HookError",
    "load_hooks",
]


def _load_app():
    return import_module("lpm.app")


def main(argv=None):
    """Proxy to :func:`lpm.app.main` without importing heavy dependencies."""

    return _load_app().main(argv)


def get_runtime_metadata():
    """Proxy to :func:`lpm.app.get_runtime_metadata`."""

    return _load_app().get_runtime_metadata()


def __getattr__(name: str) -> Any:
    if name == "ResolutionError":
        return getattr(_load_app(), name)
    raise AttributeError(name)
