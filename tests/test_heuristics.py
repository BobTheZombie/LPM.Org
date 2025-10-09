import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from src import CNF, CDCLSolver
import src.lpm.resolver as solver_module


def make_unsat_cnf():
    cnf = CNF()
    a = cnf.new_var('A')
    b = cnf.new_var('B')
    cnf.add_clause([a, b])
    cnf.add_clause([-a, b])
    cnf.add_clause([a, -b])
    cnf.add_clause([-a, -b])
    return cnf


@pytest.mark.heuristics
def test_restart_behavior(monkeypatch):
    monkeypatch.setattr(solver_module, 'luby', lambda n: 0.01)
    cnf = make_unsat_cnf()
    solver = CDCLSolver(cnf)
    solver.solve([])
    assert solver.last_restarts > 0


@pytest.mark.heuristics
def test_phase_saving():
    cnf = CNF()
    v = cnf.new_var('A')
    solver = CDCLSolver(cnf)
    res1 = solver.solve([])
    assert res1.assign[v] is True
    solver.saved_phase[v] = False
    res2 = solver.solve([])
    assert res2.assign[v] is False
