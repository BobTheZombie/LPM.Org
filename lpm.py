#!/usr/bin/env python3
"""Compatibility shim that exposes the heavy :mod:`lpm.app` API at ``import lpm``."""

from __future__ import annotations

import importlib
import importlib.util
from importlib import resources as importlib_resources
import os
import sys
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType

_SCRIPT_PATH = Path(__file__).resolve()
_SCRIPT_DIR = _SCRIPT_PATH.parent


def _iter_site_packages_roots(base: Path) -> list[Path]:
    """Return candidate ``site-packages``-style directories under *base*.

    Distribution artifacts produced by tools such as PyInstaller or zipapp
    often ship the :mod:`lpm` package under a conventional ``usr/lib`` or
    ``lib`` hierarchy that mirrors a Python installation.  We need to locate
    these directories at runtime so the lightweight ``lpm.py`` shim can import
    the real package without relying on ``sys.path`` containing the project
    root.  The helper walks a couple of common layouts and returns any
    directory that *might* contain third-party modules.
    """

    candidates: list[Path] = []
    for prefix in (base, base / "usr"):
        for lib_name in ("lib", "lib64"):
            lib_dir = prefix / lib_name
            if not lib_dir.is_dir():
                continue
            for entry in lib_dir.iterdir():
                if not entry.is_dir():
                    continue
                name = entry.name
                if not name.startswith("python"):
                    continue
                for site_name in ("site-packages", "dist-packages"):
                    site_dir = entry / site_name
                    if site_dir.is_dir():
                        candidates.append(site_dir)
    return candidates


def _iter_package_roots() -> list[Path]:
    """Generate plausible locations for the :mod:`lpm` package.

    The shim primarily targets editable checkouts where the project sources
    live under ``src/lpm``.  However, binary distributions produced by the
    project's tooling place the package underneath a staged prefix such as
    ``usr/lib/python3.11/site-packages`` next to the ``lpm`` entry-point
    script.  Scanning a broader set of directories keeps the shim functional
    in these environments without requiring additional import path tweaks.
    """

    roots: list[Path] = []
    seen: set[Path] = set()

    def _add(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        roots.append(resolved)

    # Editable/source checkouts.
    _add(_SCRIPT_DIR / "src" / "lpm")
    _add(_SCRIPT_DIR / "lpm")

    # Any staged prefixes near the shim itself.
    for parent in [_SCRIPT_DIR, *_SCRIPT_DIR.parents]:
        for site_dir in _iter_site_packages_roots(parent):
            _add(site_dir / "lpm")

    return roots


_CANDIDATE_ROOTS = _iter_package_roots()
_PRESERVED_ATTRS = ("ResolutionError",)


def _load_from_spec(spec: ModuleSpec) -> ModuleType | None:
    if not spec.loader:
        return None
    module = importlib.util.module_from_spec(spec)
    previous = sys.modules.get(__name__)
    try:
        sys.modules[__name__] = module
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop(__name__, None)
        else:
            sys.modules[__name__] = previous
    module.__file__ = __file__
    return module


def _load_package() -> ModuleType:
    for root in _CANDIDATE_ROOTS:
        init = root / "__init__.py"
        if not init.is_file():
            continue
        spec = importlib.util.spec_from_file_location(
            __name__, init, submodule_search_locations=[str(root)]
        )
        if spec:
            module = _load_from_spec(spec)
            if module is not None:
                return module

    def _spec_for(name: str) -> ModuleSpec | None:
        spec = importlib.util.find_spec(name)
        if not spec:
            return None
        origin = getattr(spec, "origin", None)
        if origin and Path(origin) == Path(__file__).resolve():
            return None
        return spec

    for candidate in ("lpm", "src.lpm"):
        spec = _spec_for(candidate)
        if not spec:
            continue
        module = _load_from_spec(spec)
        if module is None:
            continue
        if candidate != __name__:
            sys.modules.setdefault(__name__, module)
        return module

    raise ImportError("Unable to locate the LPM package")


_PACKAGE = _load_package()
__spec__ = _PACKAGE.__spec__
__package__ = _PACKAGE.__package__
__path__ = list(getattr(_PACKAGE, "__path__", []))
globals().update(
    {
        name: value
        for name, value in _PACKAGE.__dict__.items()
        if name not in {"__name__", "__package__", "__loader__", "__spec__", "__file__"}
    }
)


def _load_module_from_path(fullname: str, path: Path) -> ModuleType | None:
    if not path.is_file():
        return None

    spec = importlib.util.spec_from_file_location(fullname, path)
    if not spec or not spec.loader:
        return None

    module = importlib.util.module_from_spec(spec)
    sys.modules[fullname] = module
    spec.loader.exec_module(module)
    module.__file__ = str(path)
    return module


def _load_app_from_package(package: ModuleType) -> ModuleType | None:
    qualified_name = f"{package.__name__}.app"

    try:
        package_files = importlib_resources.files(package)
    except (AttributeError, TypeError):  # pragma: no cover - package without resources
        package_files = None

    if package_files is not None:
        candidate = package_files / "app.py"
        try:
            with importlib_resources.as_file(candidate) as path:
                module = _load_module_from_path(qualified_name, Path(path))
            if module is not None:
                return module
        except FileNotFoundError:
            pass

    package_file = getattr(package, "__file__", None)
    if package_file:
        package_path = Path(package_file)
        if package_path.name == "__init__.py":
            candidate_path = package_path.parent / "app.py"
        else:
            candidate_path = package_path.with_name("app.py")
        module = _load_module_from_path(qualified_name, candidate_path)
        if module is not None:
            return module

    return None


if "lpm.app" in sys.modules:
    previous = sys.modules["lpm.app"]
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
    try:
        _app = importlib.import_module("lpm.app")
    except ModuleNotFoundError:
        _app = _load_app_from_package(_PACKAGE)
        if _app is None:
            raise

_APP_MODULE = _app

_fs_ops = importlib.import_module("lpm.fs_ops")
_atomic_io = importlib.import_module("lpm.atomic_io")
_privileges = importlib.import_module("lpm.privileges")
_installpkg_mod = importlib.import_module("lpm.installpkg")
setattr(_app, "fs_ops", _fs_ops)
setattr(_app, "atomic_io", _atomic_io)
setattr(_app, "privileges", _privileges)
sys.modules.setdefault("lpm.fs_ops", _fs_ops)
sys.modules.setdefault("lpm.atomic_io", _atomic_io)
sys.modules.setdefault("lpm.privileges", _privileges)
sys.modules.setdefault("lpm.installpkg", _installpkg_mod)

globals().update(
    {
        name: value
        for name, value in _app.__dict__.items()
        if name not in {"__name__", "__package__", "__loader__", "__spec__", "__file__"}
    }
)
setattr(_app, "__file__", __file__)


def __getattr__(name: str):  # pragma: no cover - passthrough to app module
    try:
        return getattr(_APP_MODULE, name)
    except AttributeError as exc:  # pragma: no cover - mirror default behaviour
        raise AttributeError(name) from exc


if __name__ == "__main__":  # pragma: no cover - CLI passthrough
    raise SystemExit(_APP_MODULE.main())
