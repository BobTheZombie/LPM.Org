"""Core LPM package exposing the primary APIs used by the CLI and tests."""

from importlib import import_module
from typing import Any

from .resolver import CDCLSolver, CNF, Implication, SATResult
from .hooks import Hook, HookAction, HookError, HookTransactionManager, HookTrigger, load_hooks

__all__ = [
    "main",
    "ResolutionError",
    "get_runtime_metadata",
    "fs_ops",
    "atomic_io",
    "privileges",
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


_MODULE_PREFIXES = ("lpm", "src.lpm")


def _import_with_fallback(module: str):
    last_exc: Exception | None = None
    for prefix in _MODULE_PREFIXES:
        try:
            return import_module(f"{prefix}.{module}")
        except ModuleNotFoundError as exc:
            last_exc = exc
    raise last_exc  # type: ignore[misc]


def _load_app():
    return _import_with_fallback("app")


def main(argv=None):
    return _load_app().main(argv)


def get_runtime_metadata():
    return _load_app().get_runtime_metadata()


def __getattr__(name: str) -> Any:
    if name == "ResolutionError":
        return getattr(_load_app(), name)
    if name in {"fs_ops", "atomic_io", "privileges"}:
        return import_module(f"{__name__}.{name}")
    app = _load_app()
    try:
        return getattr(app, name)
    except AttributeError:
        raise AttributeError(name) from None
