import os
import sys
from itertools import product

import hypothesis.strategies as st
from hypothesis import given, settings

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from lpm import CNF, CDCLSolver


def cnf_strategy():
    def build(nvars):
        var_ids = st.integers(min_value=1, max_value=nvars)
        lit = st.builds(lambda v, sign: v if sign else -v, var_ids, st.booleans())
        clause = st.lists(lit, min_size=1, max_size=3)
        clauses = st.lists(clause, min_size=0, max_size=6)
        return clauses.map(lambda cs: (nvars, cs))
    return st.integers(min_value=1, max_value=4).flatmap(build)


@given(cnf_strategy())
@settings(max_examples=50)
def test_random_cnf_solver(data):
    nvars, clauses = data
    cnf = CNF()
    for i in range(1, nvars + 1):
        cnf.new_var(f"v{i}")
    for clause in clauses:
        cnf.add_clause(clause)
    solver = CDCLSolver(cnf)
    res = solver.solve([])

    def brute_force():
        for bits in product([False, True], repeat=nvars):
            assign = {i + 1: bits[i] for i in range(nvars)}
            if all(
                any(
                    (lit > 0 and assign[lit]) or (lit < 0 and not assign[-lit])
                    for lit in clause
                )
                for clause in clauses
            ):
                return True
        return False

    expected = brute_force()
    assert res.sat == expected
    if res.sat:
        for clause in clauses:
            assert any(
                (lit > 0 and res.assign[abs(lit)])
                or (lit < 0 and not res.assign[abs(lit)])
                for lit in clause
            )
