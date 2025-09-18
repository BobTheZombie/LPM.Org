import importlib
import sys
import types
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def import_lpm():
    original = sys.modules.get("lpm")
    stubbed_modules: dict[str, bool] = {}

    for name in ("zstandard", "tqdm"):
        if name not in sys.modules:
            module = types.ModuleType(name)
            if name == "tqdm":
                class _DummyTqdm:  # pragma: no cover - test helper
                    def __init__(self, iterable=None, **kwargs):
                        self.iterable = iterable
                        self.n = 0
                        self.total = kwargs.get("total")

                    def __iter__(self):
                        return iter(self.iterable or [])

                    def __enter__(self):
                        return self

                    def __exit__(self, exc_type, exc, tb):
                        return False

                    def update(self, *args, **kwargs):
                        return None

                module.tqdm = _DummyTqdm  # type: ignore[attr-defined]
            sys.modules[name] = module
            stubbed_modules[name] = True

    def _import():
        sys.modules.pop("lpm", None)
        return importlib.import_module("lpm")

    try:
        yield _import
    finally:
        if original is not None:
            sys.modules["lpm"] = original
        else:
            sys.modules.pop("lpm", None)

        for name, created in stubbed_modules.items():
            if created:
                sys.modules.pop(name, None)


def test_get_runtime_metadata_defaults(monkeypatch, import_lpm):
    monkeypatch.delenv("LPM_VERSION", raising=False)
    monkeypatch.delenv("LPM_BUILD", raising=False)
    monkeypatch.delenv("LPM_BUILD_DATE", raising=False)

    mod = import_lpm()
    metadata = mod.get_runtime_metadata()

    assert set(metadata) == {"version", "build", "build_date"}
    assert metadata["version"] == mod.__version__
    assert metadata["build"] == mod.__build__
    assert metadata["build_date"] == mod.__build_date__


def test_get_runtime_metadata_env_override(monkeypatch, import_lpm):
    monkeypatch.setenv("LPM_VERSION", "1.2.3")
    monkeypatch.setenv("LPM_BUILD", "abc123")
    monkeypatch.setenv("LPM_BUILD_DATE", "2024-01-02T03:04:05Z")

    mod = import_lpm()
    metadata = mod.get_runtime_metadata()

    assert metadata == {
        "version": "1.2.3",
        "build": "abc123",
        "build_date": "2024-01-02T03:04:05Z",
    }
    assert mod.__version__ == "1.2.3"
    assert mod.__build__ == "abc123"
    assert mod.__build_date__ == "2024-01-02T03:04:05Z"
