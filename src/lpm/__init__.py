"""Core LPM package exposing the primary APIs used by the CLI and tests."""

from importlib import import_module
from typing import Any, Iterable

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


def _load_app():
    return import_module("src.lpm.app")


def main(argv: Iterable[str] | None = None) -> int:
    app = _load_app()
    if argv is None:
        return app.main()
    return app.main(list(argv))


def get_runtime_metadata():
    return _load_app().get_runtime_metadata()


def __getattr__(name: str) -> Any:
    if name == "ResolutionError":
        return getattr(_load_app(), name)
    raise AttributeError(name)
