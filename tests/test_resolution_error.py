import pytest
from lpm import solve, Universe, ResolutionError


def test_solve_raises_resolution_error_for_missing_provider():
    u = Universe({}, {}, {}, {}, set())
    with pytest.raises(ResolutionError):
        solve(["nonexistent"], u)
