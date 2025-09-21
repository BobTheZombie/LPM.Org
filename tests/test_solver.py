import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from src import CNF, CDCLSolver


def test_conflicting_packages_unsat():
    cnf = CNF()
    a = cnf.new_var('A')
    b = cnf.new_var('B')
    c = cnf.new_var('C')
    # A -> B
    cnf.add_clause([-a, b])
    # B conflicts with C
    cnf.add_clause([-b, -c])
    # require A and C simultaneously
    cnf.add_clause([a])
    cnf.add_clause([c])
    solver = CDCLSolver(cnf)
    res = solver.solve([])
    assert not res.sat
    core_names = sorted(cnf.varname[abs(l)] for l in res.unsat_core)
    assert core_names == ["A", "C"]


def test_dependency_resolution():
    cnf = CNF()
    a = cnf.new_var('A')
    b = cnf.new_var('B')
    c = cnf.new_var('C')
    # A -> (B or C)
    cnf.add_clause([-a, b, c])
    # B conflicts C
    cnf.add_clause([-b, -c])
    cnf.add_clause([a])
    solver = CDCLSolver(cnf)
    res = solver.solve([])
    assert res.sat
    assert res.assign[a]
    # exactly one of b or c is installed
    assert res.assign[b] ^ res.assign[c]


def test_vsids_decay_map_sat_regression():
    cnf = CNF()
    a = cnf.new_var('A')
    b = cnf.new_var('B')
    # a XOR b with a forced true; solver should deduce b is false after a conflict
    cnf.add_clause([a, b])
    cnf.add_clause([-a, -b])
    cnf.add_clause([a])
    decay_map = {a: 0.6, b: 0.8}
    solver = CDCLSolver(cnf, decay_map=decay_map)
    res = solver.solve([])
    assert res.sat
    assert res.assign[a]
    assert not res.assign[b]


def test_vsids_unsat_regression():
    cnf = CNF()
    a = cnf.new_var('A')
    b = cnf.new_var('B')
    # Unsatisfiable combination requiring a branching conflict
    cnf.add_clause([a, b])
    cnf.add_clause([-a, b])
    cnf.add_clause([a, -b])
    cnf.add_clause([-a, -b])
    decay_map = {a: 0.7, b: 0.85}
    solver = CDCLSolver(cnf, decay_map=decay_map)
    res = solver.solve([])
    assert not res.sat
