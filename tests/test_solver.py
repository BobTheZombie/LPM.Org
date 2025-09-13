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
