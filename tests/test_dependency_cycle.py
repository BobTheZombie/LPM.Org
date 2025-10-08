import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "zstandard" not in sys.modules:
    module = types.ModuleType("zstandard")

    class _Passthrough:
        def __init__(self, *args, **kwargs):
            pass

        def compress(self, data):
            return data

        def decompress(self, data):
            return data

    module.ZstdCompressor = _Passthrough
    module.ZstdDecompressor = _Passthrough
    sys.modules["zstandard"] = module

if "tqdm" not in sys.modules:
    module = types.ModuleType("tqdm")

    class _DummyTqdm:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def update(self, *args, **kwargs):
            pass

    module.tqdm = _DummyTqdm
    sys.modules["tqdm"] = module

from lpm import PkgMeta, ResolutionError, Universe, solve


def test_solve_reports_dependency_cycle():
    pkg_a = PkgMeta(name="a", version="1.0", requires=["b"])
    pkg_b = PkgMeta(name="b", version="1.0", requires=["a"])
    candidates = {"a": [pkg_a], "b": [pkg_b]}
    providers = {"a": [pkg_a], "b": [pkg_b]}
    universe = Universe(candidates, providers, {}, {}, set())

    with pytest.raises(ResolutionError) as excinfo:
        solve(["a"], universe)

    message = str(excinfo.value)
    assert "Dependency cycle detected" in message
    assert "a==1.0" in message and "b==1.0" in message
