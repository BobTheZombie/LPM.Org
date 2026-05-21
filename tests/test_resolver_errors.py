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


def test_unsat_conflict_details_include_conflicting_pair():
    universe = Universe(
        candidates_by_name={},
        providers={},
        installed={},
        pins={},
        holds=set(),
    )
    a = PkgMeta(name="A", version="1.0", conflicts=["B"])
    b = PkgMeta(name="B", version="1.0")
    register_universe_candidate(universe, a)
    register_universe_candidate(universe, b)

    with pytest.raises(ResolutionError) as excinfo:
        solve(["A", "B"], universe)

    msg = str(excinfo.value)
    assert "Unsatisfiable dependency set involving: A, B" in msg
    assert "conflicts: A ↔ B" in msg


def test_unsat_details_include_dependency_cycle():
    universe = Universe(
        candidates_by_name={},
        providers={},
        installed={},
        pins={},
        holds=set(),
    )
    a = PkgMeta(name="A", version="1.0", requires=["B"])
    b = PkgMeta(name="B", version="1.0", requires=["C"])
    c = PkgMeta(name="C", version="1.0", requires=["A"], conflicts=["B"])
    register_universe_candidate(universe, a)
    register_universe_candidate(universe, b)
    register_universe_candidate(universe, c)

    with pytest.raises(ResolutionError) as excinfo:
        solve(["A", "B"], universe)

    msg = str(excinfo.value)
    assert "Unsatisfiable dependency set involving" in msg
    assert "dependency cycle:" in msg
