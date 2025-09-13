import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from src import CNF, CDCLSolver


def chain(n):
    cnf = CNF()
    vars = [cnf.new_var(f"P{i}") for i in range(n)]
    for i in range(n-1):
        cnf.add_clause([-vars[i], vars[i+1]])
    cnf.add_clause([vars[0]])
    return cnf


def run():
    for size in (50, 100, 150):
        cnf = chain(size)
        start = time.time()
        solver = CDCLSolver()
        solver.solve(cnf, set(), set())
        dur = time.time() - start
        print(f"chain {size}: {dur:.4f}s")


if __name__ == "__main__":
    run()
