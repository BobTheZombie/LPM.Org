import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from lpm import CNF, CDCLSolver


def make_unsat_cnf():
    cnf = CNF()
    a = cnf.new_var('A')
    b = cnf.new_var('B')
    cnf.add_clause([a, b])
    cnf.add_clause([-a, b])
    cnf.add_clause([a, -b])
    cnf.add_clause([-a, -b])
    return cnf


@pytest.mark.benchmark
def test_learned_clause_benchmark():
    cnf1 = make_unsat_cnf()
    solver1 = CDCLSolver(cnf1)
    solver1.solve([])
    conflicts_without = solver1.last_conflicts

    cnf2 = make_unsat_cnf()
    solver2 = CDCLSolver(cnf2)
    solver2.solve([])
    solver2.solve([])
    conflicts_with = solver2.last_conflicts

    assert conflicts_with <= conflicts_without
