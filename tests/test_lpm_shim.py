from __future__ import annotations
import importlib.machinery
import importlib.util
import sys
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType


def test_shim_falls_back_to_find_spec(monkeypatch):
    shim_path = Path(__file__).resolve().parents[1] / "lpm.py"
    loader = importlib.machinery.SourceFileLoader("lpm_shim_test", str(shim_path))
    spec = importlib.util.spec_from_loader("lpm_shim_test", loader)
    module = importlib.util.module_from_spec(spec)

    class DummyLoader:
        def create_module(self, spec):  # pragma: no cover - default semantics
            return None

        def exec_module(self, module):  # pragma: no cover - exercised via shim
            module.__dict__.update(
                {
                    "__package__": "lpm",
                    "__path__": ["dummy"],
                    "main": lambda argv=None: 0,
                    "ResolutionError": RuntimeError,
                }
            )

    dummy_spec = ModuleSpec(name="lpm", loader=DummyLoader())
    dummy_spec.origin = str(shim_path.with_name("dummy_init.py"))
    dummy_spec.submodule_search_locations = ["dummy"]

    def fake_find_spec(name: str):
        if name == "lpm":
            return dummy_spec
        return None

    dummy_app = ModuleType("lpm.app")
    dummy_app.__dict__.update({"__package__": "lpm", "main": lambda: 0})
    sys.modules["lpm.app"] = dummy_app

    def fake_import_module(name: str, package: str | None = None):
        if name == "lpm.app":
            return dummy_app
        if name in {"lpm.fs_ops", "lpm.atomic_io", "lpm.privileges", "lpm.installpkg"}:
            module = ModuleType(name)
            sys.modules[name] = module
            return module
        raise ImportError(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda *args, **kwargs: None)
    monkeypatch.setattr(importlib, "import_module", fake_import_module)
    monkeypatch.setenv("LPM_SHIM_DISABLE_RELOAD", "1")

    try:
        loader.exec_module(module)
        assert module._APP_MODULE.__name__ == "lpm.app"
    finally:
        for name in [
            "lpm_shim_test",
            "lpm.app",
            "lpm.fs_ops",
            "lpm.atomic_io",
            "lpm.privileges",
            "lpm.installpkg",
        ]:
            sys.modules.pop(name, None)


def test_shim_loads_app_from_package_when_import_fails(monkeypatch):
    shim_name = "lpm_shim_fallback"
    shim_path = Path(__file__).resolve().parents[1] / "lpm.py"
    loader = importlib.machinery.SourceFileLoader(shim_name, str(shim_path))
    spec = importlib.util.spec_from_loader(shim_name, loader)
    module = importlib.util.module_from_spec(spec)

    real_import_module = importlib.import_module

    def fake_import_module(name: str, package: str | None = None):
        if name == "lpm.app":
            raise ModuleNotFoundError(name)
        return real_import_module(name, package)

    monkeypatch.setattr(importlib, "import_module", fake_import_module)
    monkeypatch.delenv("LPM_SHIM_DISABLE_RELOAD", raising=False)

    try:
        loader.exec_module(module)
        assert module._APP_MODULE.__name__ == "lpm.app"
        assert "lpm.app" in sys.modules
    finally:
        for name in [
            shim_name,
            "lpm.app",
            "lpm.fs_ops",
            "lpm.atomic_io",
            "lpm.privileges",
            "lpm.installpkg",
        ]:
            sys.modules.pop(name, None)


def test_shim_ignores_find_spec_value_error(monkeypatch):
    shim_name = "lpm_shim_value_error"
    shim_path = Path(__file__).resolve().parents[1] / "lpm.py"
    loader = importlib.machinery.SourceFileLoader(shim_name, str(shim_path))
    spec = importlib.util.spec_from_loader(shim_name, loader)
    module = importlib.util.module_from_spec(spec)

    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str):
        if name == shim_name:
            raise ValueError("__main__.__spec__ is None")
        return real_find_spec(name)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    monkeypatch.setenv("LPM_SHIM_DISABLE_RELOAD", "1")

    try:
        loader.exec_module(module)
        assert module._APP_MODULE.__name__ == "lpm.app"
    finally:
        for name in [
            shim_name,
            "lpm",
            "lpm.app",
            "lpm.fs_ops",
            "lpm.atomic_io",
            "lpm.privileges",
            "lpm.installpkg",
        ]:
            sys.modules.pop(name, None)
