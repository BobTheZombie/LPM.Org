use std::collections::{HashMap, HashSet, VecDeque};

use rand::seq::SliceRandom;
use rand::thread_rng;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Literal(pub i32);

impl Literal {
    pub fn var(self) -> usize {
        self.0.unsigned_abs() as usize
    }

    pub fn is_positive(self) -> bool {
        self.0 > 0
    }

    pub fn negate(self) -> Self {
        Literal(-self.0)
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Clause {
    pub lits: Vec<Literal>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CNF {
    pub clauses: Vec<Clause>,
    pub num_vars: usize,
}

impl CNF {
    pub fn new(num_vars: usize) -> Self {
        Self {
            clauses: Vec::new(),
            num_vars,
        }
    }

    pub fn add_clause(&mut self, clause: Clause) {
        self.clauses.push(clause);
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Implication {
    pub var: usize,
    pub level: usize,
    pub antecedent: Option<usize>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub enum SATResult {
    Sat(Vec<bool>),
    Unsat(Vec<usize>),
    Unknown,
}

#[derive(Debug)]
pub struct CDCLSolver {
    pub cnf: CNF,
    assignments: Vec<Option<bool>>, // index 0 unused
    decision_level: usize,
    implication_graph: Vec<Option<Implication>>, // index by var
    learnt: Vec<Clause>,
}

impl CDCLSolver {
    pub fn new(num_vars: usize) -> Self {
        Self {
            cnf: CNF::new(num_vars),
            assignments: vec![None; num_vars + 1],
            decision_level: 0,
            implication_graph: vec![None; num_vars + 1],
            learnt: Vec::new(),
        }
    }

    pub fn add_clause(&mut self, lits: Vec<Literal>) {
        self.cnf.add_clause(Clause { lits });
    }

    pub fn solve(&mut self) -> SATResult {
        loop {
            if let Some(conflict_clause) = self.propagate() {
                if self.decision_level == 0 {
                    let unsat_core = conflict_clause.lits.iter().map(|l| l.var()).collect();
                    return SATResult::Unsat(unsat_core);
                }
                self.analyze_conflict(conflict_clause);
            } else if self.assignments.iter().skip(1).all(|a| a.is_some()) {
                let model = self.assignments.iter().skip(1).map(|v| v.unwrap()).collect();
                return SATResult::Sat(model);
            } else {
                self.decide();
            }
        }
    }

    fn propagate(&mut self) -> Option<Clause> {
        let mut queue: VecDeque<Literal> = self
            .assignments
            .iter()
            .enumerate()
            .filter_map(|(idx, value)| value.map(|v| Literal(if v { idx as i32 } else { -(idx as i32) })))
            .collect();

        while let Some(lit) = queue.pop_front() {
            for clause in self.cnf.clauses.iter().chain(self.learnt.iter()) {
                let mut unassigned = None;
                let mut satisfied = false;
                let mut conflict = true;
                for &c_lit in &clause.lits {
                    match self.assignments[c_lit.var()] {
                        Some(val) if val == c_lit.is_positive() => {
                            satisfied = true;
                            break;
                        }
                        Some(_) => {
                            conflict = false;
                        }
                        None => {
                            conflict = false;
                            if unassigned.is_none() {
                                unassigned = Some(c_lit);
                            }
                        }
                    }
                }

                if satisfied {
                    continue;
                }

                if conflict && unassigned.is_none() {
                    return Some(clause.clone());
                }

                if let Some(unit) = unassigned {
                    self.assign_literal(unit, Some(clause.clone()));
                    queue.push_back(unit);
                }
            }
        }

        None
    }

    fn assign_literal(&mut self, lit: Literal, antecedent: Option<Clause>) {
        let var = lit.var();
        let value = lit.is_positive();
        if self.assignments[var].is_some() {
            return;
        }
        self.assignments[var] = Some(value);
        self.implication_graph[var] = Some(Implication {
            var,
            level: self.decision_level,
            antecedent: antecedent.map(|c| self.learn_clause(c)),
        });
    }

    fn analyze_conflict(&mut self, conflict: Clause) {
        let backtrack_level = self.decision_level.saturating_sub(1);
        self.decision_level = backtrack_level;
        for val in self.assignments.iter_mut().skip(1) {
            if let Some(_) = val.take() {}
        }
        self.learnt.push(conflict);
    }

    fn decide(&mut self) {
        self.decision_level += 1;
        let mut vars: Vec<usize> = (1..=self.cnf.num_vars).collect();
        vars.shuffle(&mut thread_rng());
        for var in vars {
            if self.assignments[var].is_none() {
                self.assign_literal(Literal(var as i32), None);
                break;
            }
        }
    }

    fn learn_clause(&mut self, clause: Clause) -> usize {
        self.learnt.push(clause);
        self.learnt.len() - 1
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn adds_and_solves_simple_clause_set() {
        let mut solver = CDCLSolver::new(2);
        solver.add_clause(vec![Literal(1), Literal(2)]);
        solver.add_clause(vec![Literal(-1)]);
        let res = solver.solve();
        match res {
            SATResult::Sat(model) => {
                assert_eq!(model.len(), 2);
                assert!(!model[0]);
            }
            _ => panic!("expected SAT"),
        }
    }

    #[test]
    fn detects_unsat_core() {
        let mut solver = CDCLSolver::new(1);
        solver.add_clause(vec![Literal(1)]);
        solver.add_clause(vec![Literal(-1)]);
        let res = solver.solve();
        match res {
            SATResult::Unsat(core) => assert!(core.contains(&1)),
            _ => panic!("expected unsat"),
        }
    }

    #[test]
    fn learns_clause_on_conflict() {
        let mut solver = CDCLSolver::new(1);
        solver.add_clause(vec![Literal(1)]);
        solver.add_clause(vec![Literal(-1)]);
        let _ = solver.solve();
        assert!(!solver.learnt.is_empty());
    }
}
