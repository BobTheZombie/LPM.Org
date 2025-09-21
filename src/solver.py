from __future__ import annotations

from collections import deque
from heapq import heappop, heappush
from dataclasses import dataclass, field
from itertools import count
from typing import Dict, Iterable, List, Optional, Set, Tuple


class CNF:
    """Simple CNF container with watched literal management."""

    def __init__(self) -> None:
        self.clauses: List[List[int]] = []
        self.next_var = 1
        self.varname: Dict[int, str] = {}
        self.namevar: Dict[str, int] = {}
        self.watch_list: Dict[int, List[int]] = {}
        self.watchers: List[Tuple[int, int]] = []
        self.activity: List[float] = []
        self.lbd: List[int] = []
        self.learnts: Set[int] = set()

    def new_var(self, name: str) -> int:
        if name in self.namevar:
            return self.namevar[name]
        v = self.next_var
        self.next_var += 1
        self.namevar[name] = v
        self.varname[v] = name
        return v

    def add_clause(self, clause: List[int], learnt: bool = False, lbd: int = 0) -> int:
        idx = len(self.clauses)
        self.clauses.append(clause)
        self.activity.append(0.0)
        self.lbd.append(lbd)
        if learnt:
            self.learnts.add(idx)
        if not clause:
            self.watchers.append((0, 0))
            return idx
        if len(clause) == 1:
            lit = clause[0]
            self.watchers.append((lit, lit))
            self.watch_list.setdefault(lit, []).append(idx)
        else:
            a, b = clause[0], clause[1]
            self.watchers.append((a, b))
            self.watch_list.setdefault(a, []).append(idx)
            self.watch_list.setdefault(b, []).append(idx)
        return idx

    def add(self, *clauses: Iterable[int]) -> None:
        for c in clauses:
            clause = list(c)
            if clause:
                self.add_clause(clause)

    def remove_clause(self, idx: int) -> None:
        clause = self.clauses[idx]
        if not clause:
            return
        w1, w2 = self.watchers[idx]
        if w1 in self.watch_list and idx in self.watch_list[w1]:
            self.watch_list[w1].remove(idx)
            if not self.watch_list[w1]:
                del self.watch_list[w1]
        if w2 in self.watch_list and idx in self.watch_list[w2]:
            self.watch_list[w2].remove(idx)
            if not self.watch_list[w2]:
                del self.watch_list[w2]
        self.clauses[idx] = []
        self.watchers[idx] = (0, 0)
        self.activity[idx] = 0.0
        self.lbd[idx] = 0
        self.learnts.discard(idx)


class SATResult:
    def __init__(self, sat: bool, assign: Dict[int, bool], unsat_core: Optional[List[int]] = None):
        self.sat = sat
        self.assign = assign
        self.unsat_core = unsat_core


@dataclass
class Implication:
    """Node in the implication graph."""

    level: int = 0
    reason: Optional[int] = None
    preds: List[int] = field(default_factory=list)


def luby(i: int) -> int:
    """Return the i-th value of the Luby sequence."""
    k = 1
    while (1 << k) - 1 < i:
        k += 1
    if i == (1 << k) - 1:
        return 1 << (k - 1)
    return luby(i - (1 << (k - 1)) + 1)


class CDCLSolver:
    """Conflict-Driven Clause Learning SAT solver."""

    def __init__(
        self,
        cnf: CNF,
        prefer_true: Optional[Set[int]] = None,
        prefer_false: Optional[Set[int]] = None,
        bias: Optional[Dict[int, float]] = None,
        decay_map: Optional[Dict[int, float]] = None,
        var_decay: float = 0.95,
        cla_decay: float = 0.999,
        max_learnts: int = 200,
    ) -> None:
        self.cnf = cnf
        self.prefer_true = prefer_true or set()
        self.prefer_false = prefer_false or set()
        self.decay_map = decay_map or {}
        bias = bias or {}
        nvars = cnf.next_var - 1
        self.var_activity: Dict[int, float] = {i: bias.get(i, 0.0) for i in range(1, nvars + 1)}
        self.saved_phase: Dict[int, bool] = {}
        self.var_inc = 1.0
        self.var_decay_conf = var_decay
        self.cla_inc = 1.0
        self.cla_decay = cla_decay
        self.max_learnts = max_learnts
        self.var_decay_steps = 0
        self.var_last_decay_step: Dict[int, int] = {}

    def solve(self, assumptions: List[int]) -> SATResult:
        """Solve the stored CNF instance under optional assumptions."""
        cnf = self.cnf
        nvars = cnf.next_var - 1
        # ensure activity arrays cover all variables
        for v in range(1, nvars + 1):
            if v not in self.var_activity:
                self.var_activity[v] = 0.0
            if v not in self.var_last_decay_step:
                self.var_last_decay_step[v] = self.var_decay_steps

        assigns: Dict[int, Optional[bool]] = {i: None for i in range(1, nvars + 1)}
        levels: Dict[int, int] = {i: 0 for i in range(1, nvars + 1)}
        reason: Dict[int, Optional[int]] = {i: None for i in range(1, nvars + 1)}
        trail: List[int] = []
        trail_lim: List[int] = []
        queue = deque()
        branch_heap: List[Tuple[float, int, int]] = []
        heap_counter = count()
        active_vars: Set[int] = set(range(1, nvars + 1))
        imp_graph: Dict[int, Implication] = {i: Implication() for i in range(1, nvars + 1)}

        var_activity = self.var_activity
        saved_phase = self.saved_phase
        var_inc = self.var_inc
        var_decay_conf = self.var_decay_conf
        decay_map = self.decay_map
        max_learnts = self.max_learnts
        var_decay_steps = self.var_decay_steps
        var_last_decay_step = self.var_last_decay_step

        def normalize_var(v: int) -> float:
            last = var_last_decay_step.get(v, var_decay_steps)
            pending = var_decay_steps - last
            if pending:
                factor = decay_map.get(v, var_decay_conf)
                var_activity[v] *= pow(factor, pending)
                var_last_decay_step[v] = var_decay_steps
            elif v not in var_last_decay_step:
                var_last_decay_step[v] = var_decay_steps
            return var_activity[v]

        def push_var(v: int, priority: Optional[float] = None) -> None:
            if assigns[v] is not None or v not in active_vars:
                return
            if priority is None:
                priority = -normalize_var(v)
            heappush(branch_heap, (priority, next(heap_counter), v))

        for v in active_vars:
            push_var(v)

        # stats for testing/benchmarking
        self.last_conflicts = 0
        self.last_restarts = 0

        def bump_var(v: int) -> None:
            nonlocal var_inc
            normalize_var(v)
            var_activity[v] += var_inc
            if var_activity[v] > 1e100:
                for k in var_activity:
                    normalize_var(k)
                    var_activity[k] *= 1e-100
                var_inc *= 1e-100
            if assigns[v] is None and v in active_vars:
                push_var(v, -var_activity[v])

        def decay_var_activity() -> None:
            nonlocal var_inc, var_decay_steps
            var_inc /= var_decay_conf
            var_decay_steps += 1

        cla_inc = self.cla_inc
        cla_decay = self.cla_decay

        def bump_clause(ci: Optional[int]) -> None:
            if ci is not None:
                cnf.activity[ci] += cla_inc

        def decay_clause_activity() -> None:
            nonlocal cla_inc
            cla_inc /= cla_decay

        def reduce_db() -> None:
            learnts = [idx for idx in cnf.learnts if cnf.clauses[idx]]
            if len(learnts) <= max_learnts:
                return
            learnts.sort(key=lambda idx: (cnf.lbd[idx], -cnf.activity[idx]))
            reasons = set(reason.values())
            for idx in learnts[max_learnts:]:
                if idx not in reasons and len(cnf.clauses[idx]) > 2:
                    cnf.remove_clause(idx)

        def current_level() -> int:
            return len(trail_lim)

        def value(lit: int) -> Optional[bool]:
            val = assigns[abs(lit)]
            if val is None:
                return None
            return val if lit > 0 else not val

        def enqueue(lit: int, rsn: Optional[int]) -> None:
            v = abs(lit)
            val = lit > 0
            if assigns[v] is not None:
                return
            assigns[v] = val
            active_vars.discard(v)
            saved_phase[v] = val
            levels[v] = current_level()
            reason[v] = rsn
            trail.append(lit)
            queue.append(lit)
            node = imp_graph[v]
            node.level = levels[v]
            node.reason = rsn
            if rsn is not None:
                node.preds = [l for l in cnf.clauses[rsn] if abs(l) != v]
            else:
                node.preds = []

        for i, cl in enumerate(cnf.clauses):
            if len(cl) == 1:
                enqueue(cl[0], i)

        for lit in assumptions:
            enqueue(lit, None)

        def propagate() -> Optional[int]:
            while queue:
                lit = queue.popleft()
                for ci in list(cnf.watch_list.get(-lit, [])):
                    clause = cnf.clauses[ci]
                    w1, w2 = cnf.watchers[ci]
                    if w1 == -lit:
                        other = w2
                        first = True
                    else:
                        other = w1
                        first = False
                    if value(other) is True:
                        continue
                    found = False
                    for new_lit in clause:
                        if new_lit == other or new_lit == -lit:
                            continue
                        if value(new_lit) is not False:
                            if first:
                                cnf.watchers[ci] = (new_lit, other)
                            else:
                                cnf.watchers[ci] = (other, new_lit)
                            cnf.watch_list[-lit].remove(ci)
                            cnf.watch_list.setdefault(new_lit, []).append(ci)
                            found = True
                            break
                    if not found:
                        if value(other) is False:
                            return ci
                        else:
                            enqueue(other, ci)
            return None

        def pick_branch_var() -> int:
            while branch_heap:
                priority, _, v = heappop(branch_heap)
                if assigns[v] is not None or v not in active_vars:
                    continue
                current_priority = -normalize_var(v)
                if abs(current_priority - priority) > 1e-12:
                    push_var(v, current_priority)
                    continue
                return v
            return 0

        def analyze(conflict_idx: int) -> Tuple[List[int], int]:
            bump_clause(conflict_idx)
            for lit in cnf.clauses[conflict_idx]:
                bump_var(abs(lit))
            seen: Set[int] = set()
            learnt: List[int] = []
            counter = 0
            clause = cnf.clauses[conflict_idx][:]
            i = len(trail) - 1
            while True:
                for lit in clause:
                    v = abs(lit)
                    bump_var(v)
                    node = imp_graph[v]
                    if v not in seen and node.level > 0:
                        seen.add(v)
                        if node.level == current_level():
                            counter += 1
                        else:
                            learnt.append(lit)
                while True:
                    lit = trail[i]
                    i -= 1
                    if abs(lit) in seen:
                        break
                v = abs(lit)
                clause_idx = imp_graph[v].reason
                bump_clause(clause_idx)
                if clause_idx is not None:
                    for l in cnf.clauses[clause_idx]:
                        bump_var(abs(l))
                clause = imp_graph[v].preds.copy() if clause_idx is not None else []
                counter -= 1
                if counter <= 0:
                    learnt.append(-lit)
                    break
            back_lvl = 0
            if len(learnt) > 1:
                back_lvl = max(imp_graph[abs(l)].level for l in learnt[:-1])
            for lit in learnt:
                bump_var(abs(lit))
            return learnt, back_lvl

        def backtrack(level: int) -> None:
            while current_level() > level:
                start = trail_lim.pop()
                while len(trail) > start:
                    lit = trail.pop()
                    v = abs(lit)
                    assigns[v] = None
                    reason[v] = None
                    levels[v] = 0
                    imp_graph[v] = Implication()
                    active_vars.add(v)
                    push_var(v)
                queue.clear()

        conflicts = 0
        restart_count = 1
        restart_limit = luby(restart_count) * 100

        while True:
            confl = propagate()
            if confl is not None:
                conflicts += 1
                if current_level() == 0:
                    core_clause = cnf.clauses[confl][:]
                    changed = True
                    while changed:
                        changed = False
                        for lit in list(core_clause):
                            v = abs(lit)
                            rsn = reason[v]
                            if rsn is not None and len(cnf.clauses[rsn]) > 1:
                                core_clause.remove(lit)
                                for l in cnf.clauses[rsn]:
                                    if abs(l) != v and l not in core_clause:
                                        core_clause.append(l)
                                changed = True
                    self.var_inc = var_inc
                    self.var_decay_steps = var_decay_steps
                    self.cla_inc = cla_inc
                    self.last_conflicts = conflicts
                    return SATResult(False, {v: False for v in assigns}, core_clause)
                learnt, back_lvl = analyze(confl)
                lbd = len({levels[abs(l)] for l in learnt})
                ci = cnf.add_clause(learnt, learnt=True, lbd=lbd)
                bump_clause(ci)
                backtrack(back_lvl)
                enqueue(learnt[0], ci)
                decay_clause_activity()
                decay_var_activity()
                if len(cnf.learnts) > max_learnts:
                    reduce_db()
                if conflicts >= restart_limit:
                    restart_count += 1
                    restart_limit = luby(restart_count) * 100
                    self.last_restarts += 1
                    backtrack(0)
            else:
                v = pick_branch_var()
                if v == 0:
                    self.var_inc = var_inc
                    self.var_decay_steps = var_decay_steps
                    self.cla_inc = cla_inc
                    self.last_conflicts = conflicts
                    final = {var: (assigns[var] if assigns[var] is not None else False) for var in assigns}
                    return SATResult(True, final, None)
                trail_lim.append(len(trail))
                phase = saved_phase.get(v)
                if phase is None:
                    lit = -v if v in self.prefer_false and v not in self.prefer_true else v
                else:
                    lit = v if phase else -v
                enqueue(lit, None)

