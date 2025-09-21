import os
import random
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


def pigeonhole(n):
    cnf = CNF()
    holes = list(range(n))
    pigeons = list(range(n + 1))
    var_map = {
        (p, h): cnf.new_var(f"PH_{p}_{h}")
        for p in pigeons
        for h in holes
    }
    # each pigeon must be in at least one hole
    for p in pigeons:
        cnf.add_clause([var_map[(p, h)] for h in holes])
    # no two pigeons share a hole
    for h in holes:
        for i in range(len(pigeons)):
            for j in range(i + 1, len(pigeons)):
                cnf.add_clause([-var_map[(pigeons[i], h)], -var_map[(pigeons[j], h)]])
    return cnf


def planted_3sat(n_vars, n_clauses, seed):
    rng = random.Random(seed)
    cnf = CNF()
    vars = [cnf.new_var(f"X{i}") for i in range(n_vars)]
    assignment = {var: rng.choice([True, False]) for var in vars}
    for _ in range(n_clauses):
        chosen = rng.sample(vars, 3)
        satisfied_idx = rng.randrange(3)
        clause = []
        for idx, var in enumerate(chosen):
            val = assignment[var]
            if idx == satisfied_idx:
                clause.append(var if val else -var)
            else:
                clause.append(var if rng.random() < 0.5 else -var)
        cnf.add_clause(clause)
    return cnf, assignment


def verify_solution(clauses, assignment):
    for clause in clauses:
        if not clause:
            continue
        if not any(
            (lit > 0 and assignment.get(abs(lit), False))
            or (lit < 0 and not assignment.get(abs(lit), False))
            for lit in clause
        ):
            raise AssertionError("assignment does not satisfy CNF")


def benchmark_case(name, builder, expect_sat):
    cnf, expected_assignment = builder()
    original = [clause[:] for clause in cnf.clauses]
    if expected_assignment is not None:
        verify_solution(original, expected_assignment)
    start = time.perf_counter()
    solver = CDCLSolver(cnf)
    result = solver.solve([])
    duration = time.perf_counter() - start
    status = "SAT" if result.sat else "UNSAT"
    if expect_sat:
        if not result.sat:
            raise AssertionError(f"{name} expected SAT but was UNSAT")
        verify_solution(original, result.assign)
    else:
        if result.sat:
            raise AssertionError(f"{name} expected UNSAT but was SAT")
    print(
        f"{name}: {status} in {duration:.4f}s with {solver.last_conflicts} conflicts"
    )
    return duration


def run():
    for size in (50, 100, 150):
        benchmark_case(
            f"chain-{size}",
            lambda s=size: (chain(s), None),
            expect_sat=True,
        )

    for holes in (4, 5):
        benchmark_case(
            f"pigeonhole-{holes}",
            lambda n=holes: (pigeonhole(n), None),
            expect_sat=False,
        )

    planted_params = ((40, 160, 0), (60, 260, 1), (80, 360, 2))
    for n_vars, n_clauses, seed in planted_params:
        benchmark_case(
            f"planted3sat-{n_vars}v-{n_clauses}c",
            lambda nv=n_vars, nc=n_clauses, sd=seed: planted_3sat(nv, nc, sd),
            expect_sat=True,
        )


if __name__ == "__main__":
    run()
