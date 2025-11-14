"""Core LPM package exposing the primary APIs used by the CLI and tests."""

from functools import lru_cache
from importlib import import_module
from types import ModuleType
from typing import Any, Iterable

from .cli import main as _cli_main
from .hooks import Hook, HookAction, HookError, HookTransactionManager, HookTrigger, load_hooks
from .resolver import CDCLSolver, CNF, Implication, SATResult

__all__ = [
    "main",
    "ResolutionError",
    "get_runtime_metadata",
    "CNF",
    "SATResult",
    "Implication",
    "CDCLSolver",
    "Hook",
    "HookAction",
    "HookTransactionManager",
    "HookTrigger",
    "HookError",
    "load_hooks",
]


@lru_cache(maxsize=1)
def _load_app() -> ModuleType:
    """Import and cache the heavyweight :mod:`lpm.app` module."""

    return import_module(f"{__name__}.app")


def main(argv: Iterable[str] | None = None) -> int:
    if argv is None:
        return _cli_main()
    return _cli_main(list(argv))


def get_runtime_metadata():
    return _load_app().get_runtime_metadata()


def __getattr__(name: str) -> Any:
    if name == "ResolutionError":
        return getattr(_load_app(), name)
    raise AttributeError(name)
