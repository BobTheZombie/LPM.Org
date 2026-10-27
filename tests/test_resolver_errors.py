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

def test_same_version_different_releases_are_distinct_candidates():
    universe = Universe({}, {}, {}, {}, set())
    register_universe_candidate(
        universe, PkgMeta(name="libqt", version="6.12.0", release="1", repo="older")
    )
    register_universe_candidate(
        universe, PkgMeta(name="libqt", version="6.12.0", release="2", repo="newer")
    )
    from src.lpm.app import encode_resolution, parse_dep_expr
    cnf, var_of, *_ = encode_resolution(
        universe, [parse_dep_expr("libqt")]
    )
    assert len(var_of) == 2
    assert cnf.next_var >= 4
    chosen = solve(["libqt"], universe)
    assert len(chosen) == 1
    assert chosen[0].name == "libqt"


def test_irrelevant_package_is_not_selected():
    universe = Universe({}, {}, {}, {}, set())
    register_universe_candidate(universe, PkgMeta(name="requested", version="1"))
    register_universe_candidate(universe, PkgMeta(name="unrelated", version="1"))
    assert [p.name for p in solve(["requested"], universe)] == ["requested"]


def test_holds_reject_incompatible_candidate_updates():
    universe = Universe(
        {}, {}, {"libqt": {"version": "6.11", "release": "1", "provides": []}},
        {}, {"libqt"}
    )
    register_universe_candidate(
        universe, PkgMeta(name="libqt", version="6.12", release="1")
    )
    assert solve(["libqt"], universe) == []
    with pytest.raises(ResolutionError):
        solve(["libqt>=6.12"], universe)


def test_pin_with_no_matching_version_is_not_ignored():
    universe = Universe({}, {}, {}, {"libqt": "==6.12"}, set())
    register_universe_candidate(
        universe, PkgMeta(name="libqt", version="6.11")
    )
    with pytest.raises(ResolutionError):
        solve(["libqt"], universe)


def test_virtual_provides_use_advertised_version():
    universe = Universe({}, {}, {}, {}, set())
    register_universe_candidate(
        universe, PkgMeta(
            name="engine", version="99.0", provides=["virtual-abi=2.0"]
        )
    )
    assert [p.name for p in solve(["virtual-abi>=2"], universe)] == ["engine"]
    with pytest.raises(ResolutionError):
        solve(["virtual-abi>=3"], universe)


def test_unsat_reports_conflict_with_installed_package():
    universe = Universe(
        {}, {}, {"old-audio": {"version": "1.0", "provides": []}}, {}, set()
    )
    register_universe_candidate(
        universe, PkgMeta(name="desktop", version="1.0", requires=["new-audio"])
    )
    register_universe_candidate(
        universe, PkgMeta(name="new-audio", version="2.0", conflicts=["old-audio"])
    )
    with pytest.raises(ResolutionError) as excinfo:
        solve(["desktop"], universe, include_build_requires=True)
    assert "new-audio ↔ installed old-audio" in str(excinfo.value)


def test_unsat_reports_pin_that_blocks_transitive_source_dependency():
    universe = Universe({}, {}, {}, {"framework": "==1.0"}, set())
    register_universe_candidate(
        universe, PkgMeta(name="desktop", version="1.0", requires=["framework>=2"])
    )
    register_universe_candidate(
        universe, PkgMeta(name="framework", version="2.0")
    )
    with pytest.raises(ResolutionError) as excinfo:
        solve(["desktop"], universe)
    assert "framework pinned to ==1.0" in str(excinfo.value)


def test_unsat_reports_conflicting_transitive_version_requirements():
    universe = Universe({}, {}, {}, {}, set())
    register_universe_candidate(
        universe, PkgMeta(name="plasma6-full", version="6.8.1", requires=["first", "second"])
    )
    register_universe_candidate(
        universe, PkgMeta(name="first", version="1", requires=["qt6-base>=6.12.0"])
    )
    register_universe_candidate(
        universe, PkgMeta(name="second", version="1", requires=["qt6-base<6.12.0"])
    )
    register_universe_candidate(
        universe, PkgMeta(name="qt6-base", version="6.11.2")
    )
    register_universe_candidate(
        universe, PkgMeta(name="qt6-base", version="6.12.0")
    )
    with pytest.raises(ResolutionError) as excinfo:
        solve(["plasma6-full"], universe)
    msg = str(excinfo.value)
    assert "possible version mismatches:" in msg
    assert "first requires qt6-base>=6.12.0" in msg
    assert "second requires qt6-base<6.12.0" in msg
