import importlib
import json
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def import_lpm():
    original = sys.modules.get("lpm")
    stubbed_modules: dict[str, bool] = {}

    if "tqdm" not in sys.modules:
        import types

        module = types.ModuleType("tqdm")

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
        sys.modules["tqdm"] = module
        stubbed_modules["tqdm"] = True

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
    monkeypatch.delenv("LPM_NAME", raising=False)
    monkeypatch.delenv("LPM_VERSION", raising=False)
    monkeypatch.delenv("LPM_BUILD", raising=False)
    monkeypatch.delenv("LPM_BUILD_DATE", raising=False)
    monkeypatch.delenv("LPM_DEVELOPER", raising=False)
    monkeypatch.delenv("LPM_URL", raising=False)
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)

    mod = import_lpm()
    metadata = mod.get_runtime_metadata()

    assert set(metadata) == {"name", "version", "build", "build_date", "developer", "url"}
    assert metadata["name"] == mod.__title__
    assert metadata["version"] == mod.__version__
    assert metadata["build"] == mod.__build__
    assert metadata["build_date"] == mod.__build_date__
    assert metadata["developer"] == mod.__developer__
    assert metadata["url"] == mod.__url__


def test_get_runtime_metadata_env_override(monkeypatch, import_lpm):
    monkeypatch.setenv("LPM_NAME", "Custom LPM")
    monkeypatch.setenv("LPM_VERSION", "1.2.3")
    monkeypatch.setenv("LPM_BUILD", "abc123")
    monkeypatch.setenv("LPM_BUILD_DATE", "2024-01-02T03:04:05Z")
    monkeypatch.setenv("LPM_DEVELOPER", "Jane Doe")
    monkeypatch.setenv("LPM_URL", "https://example.com/lpm")

    mod = import_lpm()
    metadata = mod.get_runtime_metadata()

    assert metadata == {
        "name": "Custom LPM",
        "version": "1.2.3",
        "build": "abc123",
        "build_date": "2024-01-02T03:04:05Z",
        "developer": "Jane Doe",
        "url": "https://example.com/lpm",
    }
    assert mod.__title__ == "Custom LPM"
    assert mod.__version__ == "1.2.3"
    assert mod.__build__ == "abc123"
    assert mod.__build_date__ == "2024-01-02T03:04:05Z"
    assert mod.__developer__ == "Jane Doe"
    assert mod.__url__ == "https://example.com/lpm"


def test_default_build_date_uses_source_date_epoch(monkeypatch, import_lpm):
    monkeypatch.delenv("LPM_BUILD_DATE", raising=False)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1700000000")

    mod = import_lpm()

    assert mod.__build_date__ == "2023-11-14T22:13:20Z"


def test_build_info_file_overrides_defaults(tmp_path, monkeypatch, import_lpm):
    monkeypatch.delenv("LPM_NAME", raising=False)
    monkeypatch.delenv("LPM_VERSION", raising=False)
    monkeypatch.delenv("LPM_BUILD", raising=False)
    monkeypatch.delenv("LPM_BUILD_DATE", raising=False)
    monkeypatch.delenv("LPM_DEVELOPER", raising=False)
    monkeypatch.delenv("LPM_URL", raising=False)
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)

    build_info = {
        "version": "2024-05-06T07:08:09Z",
        "build": "nightly",
        "build_date": "2024-05-06T07:08:09Z",
    }
    info_path = tmp_path / "build-info.json"
    info_path.write_text(json.dumps(build_info), encoding="utf-8")
    monkeypatch.setenv("LPM_BUILD_INFO", str(info_path))

    mod = import_lpm()
    metadata = mod.get_runtime_metadata()

    assert mod.__version__ == "2024-05-06T07:08:09Z"
    assert mod.__build__ == "nightly"
    assert mod.__build_date__ == "2024-05-06T07:08:09Z"
    assert metadata["version"] == "2024-05-06T07:08:09Z"
    assert metadata["build"] == "nightly"
    assert metadata["build_date"] == "2024-05-06T07:08:09Z"
