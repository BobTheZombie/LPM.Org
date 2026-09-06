"""Core LPM package exposing the primary APIs used by the CLI and tests."""

from __future__ import annotations

from importlib import import_module
import os
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    src_root = Path(__file__).resolve().parents[1]
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))
    __package__ = "lpm"

from ._compat import register as _register_facade

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
    if name in {"STATE_DIR", "CACHE_DIR", "SOURCE_CACHE_DIR"} and os.environ.get("LPM_STATE_DIR"):
        state = Path(os.environ["LPM_STATE_DIR"])
        paths = {"STATE_DIR": state, "CACHE_DIR": state / "cache", "SOURCE_CACHE_DIR": state / "cache/sources"}
        paths[name].mkdir(parents=True, exist_ok=True)
        return paths[name]
    if name == "ResolutionError":
        return getattr(_load_app(), name)
    if name in {"config", "fs_ops", "atomic_io", "privileges"}:
        return import_module(f"{__name__}.{name}")
    if name in {"__title__", "__version__", "__build__", "__build_date__", "__developer__", "__url__"}:
        app = _load_app()
        refresh = getattr(app, "_refresh_runtime_metadata", None)
        if callable(refresh):
            refresh()
        return getattr(app, name)
    app = _load_app()
    try:
        return getattr(app, name)
    except AttributeError:
        raise AttributeError(name) from None


# ``lpm.app`` may outlive this facade when embedders deliberately reload the
# public module.  Register every facade instance so app-level compatibility
# overrides (including hooks) still reach the object the caller imported.
_register_facade(sys.modules[__name__])
_existing_app = sys.modules.get("lpm.app") or sys.modules.get("src.lpm.app")
if _existing_app is not None:
    _overrides = getattr(_existing_app, "_LPM_OVERRIDE_MODULES", None)
    if isinstance(_overrides, list) and sys.modules[__name__] not in _overrides:
        _overrides.append(sys.modules[__name__])


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    if __package__ in {None, ""}:
        from pathlib import Path
        import sys

        src_root = Path(__file__).resolve().parents[1]
        if str(src_root) not in sys.path:
            sys.path.insert(0, str(src_root))
    raise SystemExit(main())
