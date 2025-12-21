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


def test_build_requires_checked_when_enabled():
    universe = Universe(
        candidates_by_name={},
        providers={},
        installed={},
        pins={},
        holds=set(),
    )
    pkg = PkgMeta(name="builder", version="1.0", build_requires=["toolchain"])
    register_universe_candidate(universe, pkg)

    with pytest.raises(ResolutionError) as excinfo:
        solve(["builder"], universe, include_build_requires=True)

    assert "toolchain" in str(excinfo.value)

    # Without the build-requires flag, the dependency should be ignored during install
    clean_universe = Universe(
        candidates_by_name={},
        providers={},
        installed={},
        pins={},
        holds=set(),
    )
    register_universe_candidate(clean_universe, pkg)

    solved = solve(["builder"], clean_universe)
    assert [p.name for p in solved] == ["builder"]
