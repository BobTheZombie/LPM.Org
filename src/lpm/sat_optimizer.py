"""Exact minimum-change SAT optimization using bounded cardinality constraints.

The optimizer first finds a feasible transaction and then binary-searches the
smallest number of changed package candidates. Unlike VSIDS polarity preferences,
this is a hard objective: each accepted plan has a proof that fewer changes are
unsatisfiable under the same dependency constraints.
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional, Set

from .resolver import CNF, CDCLSolver, SATResult


def clone_cnf(source: CNF) -> CNF:
    clone = CNF()
    for var in range(1, source.next_var):
        clone.new_var(source.varname[var])
    for idx, clause in enumerate(source.clauses):
        if clause:
            clone.add_clause(clause, learnt=idx in source.learnts, lbd=source.lbd[idx])
    if source.has_empty_clause:
        clone.add_clause([])
    return clone


def constrain_at_most(cnf: CNF, literals: Iterable[int], k: int) -> None:
    """Sinz sequential counter encoding for sum(literals) <= k.

    Uses O(n*k) auxiliary variables and clauses; no model enumeration.
    """
    xs = list(dict.fromkeys(literals))
    if any(lit <= 0 or lit >= cnf.next_var for lit in xs):
        raise ValueError("objective requires positive, allocated variables")
    if k < 0:
        cnf.add_clause([])
        return
    if k >= len(xs):
        return
    if k == 0:
        for x in xs:
            cnf.add_clause([-x])
        return
    # S[i][j] means at least j of x[0..i] are TRUE.
    rows = [
        [cnf.new_var(f"min_change_{cnf.next_var}_{i}_{j}") for j in range(k)]
        for i in range(len(xs))
    ]
    for i, x in enumerate(xs):
        cnf.add_clause([-x, rows[i][0]])
        if i == 0:
            continue
        cnf.add_clause([-x, -rows[i - 1][k - 1]])
        for j in range(k):
            cnf.add_clause([-rows[i - 1][j], rows[i][j]])
            if j:
                cnf.add_clause([-x, -rows[i - 1][j - 1], rows[i][j]])


def minimize_changes(
    cnf: CNF,
    changed_vars: Iterable[int],
    *,
    prefer_true: Optional[Set[int]] = None,
    prefer_false: Optional[Set[int]] = None,
    bias: Optional[Dict[int, float]] = None,
    decay_map: Optional[Dict[int, float]] = None,
    var_decay: float = 0.95,
    cla_decay: float = 0.999,
    max_learnts: int = 200,
) -> SATResult:
    """Solve with a globally minimum count of changed candidate variables."""
    costs = list(dict.fromkeys(changed_vars))
    kwargs = dict(
        prefer_true=prefer_true,
        prefer_false=prefer_false,
        bias=bias,
        decay_map=decay_map,
        var_decay=var_decay,
        cla_decay=cla_decay,
        max_learnts=max_learnts,
    )
    initial = CDCLSolver(cnf, **kwargs).solve([])
    if not initial.sat or not costs:
        return initial

    upper = sum(bool(initial.assign.get(v)) for v in costs)
    if upper == 0:
        return initial
    low = 0
    best = initial
    while low < upper:
        bound = (low + upper) // 2
        trial = clone_cnf(cnf)
        constrain_at_most(trial, costs, bound)
        model = CDCLSolver(trial, **kwargs).solve([])
        if model.sat:
            best = model
            upper = sum(bool(model.assign.get(v)) for v in costs)
        else:
            low = bound + 1
    return best
