import pytest

from src.lpm.app import (
    PkgMeta,
    ResolutionError,
    Universe,
    register_universe_candidate,
    solve,
)


def test_missing_dependency_reports_offending_atom():
    universe = Universe(
        candidates_by_name={},
        providers={},
        installed={},
        pins={},
        holds=set(),
    )
    pkg = PkgMeta(name="system-base", version="1.0", requires=["glibc"])
    register_universe_candidate(universe, pkg)

    with pytest.raises(ResolutionError) as excinfo:
        solve(["system-base"], universe)

    assert "No provider for dependency 'glibc'" in str(excinfo.value)
    assert "system-base-1.0" in str(excinfo.value)
