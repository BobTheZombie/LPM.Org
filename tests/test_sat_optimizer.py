"""Regressions for exact minimum-change transaction selection."""
from src.lpm.app import PkgMeta, Universe, register_universe_candidate, solve
from src.lpm.resolver import CNF
from src.lpm.sat_optimizer import constrain_at_most, minimize_changes


def empty(installed=None):
    return Universe({}, {}, installed or {}, {}, set())


def test_minimum_changes_beats_first_satisfying_model():
    universe = empty()
    register_universe_candidate(
        universe, PkgMeta(name="single-a", version="1", provides=["cap-a=1"])
    )
    register_universe_candidate(
        universe, PkgMeta(name="single-b", version="1", provides=["cap-b=1"])
    )
    register_universe_candidate(
        universe, PkgMeta(
            name="combined", version="1", provides=["cap-a=1", "cap-b=1"]
        )
    )
    result = solve(["cap-a", "cap-b"], universe)
    assert [p.name for p in result] == ["combined"]


def test_existing_package_is_not_reinstalled():
    universe = empty({"qt6-base": {"version": "6.12.0", "release": "1", "arch": "noarch"}})
    register_universe_candidate(
        universe, PkgMeta(name="qt6-base", version="6.12.0", release="1")
    )
    assert solve(["qt6-base"], universe) == []


def test_incompatible_installed_version_is_upgraded():
    universe = empty({"qt6-base": {"version": "6.11.2", "release": "1"}})
    register_universe_candidate(
        universe, PkgMeta(name="qt6-base", version="6.12.0")
    )
    chosen = solve(["qt6-base>=6.12.0"], universe)
    assert [(p.name, p.version) for p in chosen] == [("qt6-base", "6.12.0")]


def test_cardinality_bound_is_hard_not_preference():
    cnf = CNF()
    a, b, c = [cnf.new_var(name) for name in ("a", "b", "c")]
    cnf.add([a, b], [b, c])
    result = minimize_changes(cnf, [a, b, c], prefer_true={a, b, c})
    assert result.sat
    assert sum(result.assign[v] for v in (a, b, c)) == 1
    assert result.assign[b]


def test_too_tight_cardinality_bound_unsat():
    cnf = CNF()
    a, b = [cnf.new_var(name) for name in ("a", "b")]
    cnf.add([a], [b])
    constrain_at_most(cnf, [a, b], 1)
    from src.lpm.resolver import CDCLSolver
    assert not CDCLSolver(cnf).solve([]).sat
