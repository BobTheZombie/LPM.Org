"""Plan source builds with LPM's dependency SAT solver before executing recipes.

The planner uses the same dependency/version/provides/conflicts constraints as
binary transactions. Stage-2 dependencies deliberately remain in stage2.py:
they are not prerequisites of the initial bootstrap stage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from .app import (
    PkgMeta, Universe, ResolutionError, Atom, DepExpr,
    parse_dep_expr, flatten_and, flatten_or, providers_for, solve,
    register_universe_candidate, _installed_provider_map,
    _match_dep_expr_against_installed, arch_compatible,
)


@dataclass
class SourceBuildPlan:
    root: str
    recipes: Dict[str, Path] = field(default_factory=dict)
    selected: Dict[str, PkgMeta] = field(default_factory=dict)
    requirements: Dict[str, List[str]] = field(default_factory=dict)
    # Shared between recursive builds. The caller executes one dependency at a
    # time so diamond-shaped dependency graphs are not rebuilt concurrently.
    completed: Set[str] = field(default_factory=set)

    def direct_for(self, package: str) -> List[str]:
        return list(self.requirements.get(package, ()))


def _atoms(expr: DepExpr) -> Iterable[Atom]:
    if expr.kind == "atom":
        if expr.atom:
            yield expr.atom
    elif expr.left and expr.right:
        yield from _atoms(expr.left)
        yield from _atoms(expr.right)


def plan_source_build(
    root_path: Path,
    root_scalars: Mapping[str, str],
    root_arrays: Mapping[str, List[str]],
    installed: Dict[str, dict],
    fetch_recipe: Callable[[str, Path], Path],
    read_recipe: Callable[..., Tuple[dict, dict, dict]],
    *,
    force_rebuild: bool = False,
    target_arch: str = "",
) -> SourceBuildPlan:
    """Discover the source closure, solve it globally, and bind build recipes.

    Unlike name-only traversal, versioned requirements are checked by SAT.
    An unavailable alternative is allowed if another OR provider is available.
    """
    root_name = root_scalars["NAME"]
    effective_installed = {} if force_rebuild else dict(installed)
    installed_providers = _installed_provider_map(effective_installed)
    u = Universe({}, {}, effective_installed, {}, set())
    plan = SourceBuildPlan(root=root_name)
    examined: Set[str] = set()
    unavailable: Dict[str, str] = {}

    def add(name: str, path: Path, scal: Mapping[str, str], arr: Mapping[str, List[str]]) -> None:
        if name in examined:
            return
        examined.add(name)
        if not scal.get("VERSION"):
            raise ResolutionError(f"{name}: source recipe has no VERSION")
        arch = scal.get("ARCH") or "noarch"
        if target_arch and not arch_compatible(arch, target_arch):
            raise ResolutionError(f"{name}: incompatible source architecture {arch}")
        meta = PkgMeta(
            name=name, version=str(scal["VERSION"]),
            release=str(scal.get("RELEASE", "1")),
            arch=arch, requires=list(arr.get("REQUIRES", ())),
            build_requires=list(arr.get("BUILD_REQUIRES", ())),
            provides=list(arr.get("PROVIDES", ())),
            conflicts=list(arr.get("CONFLICTS", ())),
            obsoletes=list(arr.get("OBSOLETES", ())),
            recommends=list(arr.get("RECOMMENDS", ())),
            suggests=list(arr.get("SUGGESTS", ())),
        )
        register_universe_candidate(u, meta)
        plan.recipes[name] = Path(path)
        plan.requirements[name] = meta.requires + meta.build_requires

        for requirement in plan.requirements[name]:
            if not requirement:
                continue
            expr = parse_dep_expr(requirement)
            if not force_rebuild and _match_dep_expr_against_installed(
                expr, effective_installed, installed_providers
            ):
                continue
            for atom in _atoms(expr):
                dep = atom.name
                if dep in examined or dep == root_name:
                    continue
                # Only the named source recipe can be fetched without an index
                # mapping capabilities to their actual provider package names.
                if dep in unavailable:
                    continue
                dest = Path("/tmp") / f"lpm-dep-{dep}.lpmbuild"
                try:
                    fetched = Path(fetch_recipe(dep, dest))
                    sub_scal, sub_arr, _ = read_recipe(fetched)
                    actual_name = sub_scal.get("NAME", "")
                    if actual_name != dep:
                        raise ResolutionError(
                            f"{dep}: fetched recipe declares different NAME {actual_name!r}"
                        )
                    add(dep, fetched, sub_scal, sub_arr)
                except (OSError, ValueError, ResolutionError, SystemExit) as exc:
                    unavailable[dep] = str(exc)
                    continue

    add(root_name, root_path, root_scalars, root_arrays)
    selected = solve([root_name], u, include_build_requires=True)
    plan.selected = {p.name: p for p in selected}

    # Preserve the selected alternatives, not merely the first named atom.
    def choose(expr: DepExpr) -> List[str]:
        if not force_rebuild and _match_dep_expr_against_installed(
            expr, effective_installed, installed_providers
        ):
            return []
        if expr.kind == "and":
            return list(dict.fromkeys(
                name for part in flatten_and(expr) for name in choose(part)
            ))
        if expr.kind == "or":
            for part in flatten_or(expr):
                selected_names = choose(part)
                if selected_names:
                    return selected_names
            return []
        if expr.kind == "atom" and expr.atom:
            for pkg in providers_for(u, expr.atom):
                if pkg.name in plan.selected:
                    return [pkg.name]
        return []

    requirements: Dict[str, List[str]] = {}
    for name, raw in plan.requirements.items():
        selected_names: List[str] = []
        for value in raw:
            if value:
                selected_names.extend(choose(parse_dep_expr(value)))
        requirements[name] = [
            dep for dep in dict.fromkeys(selected_names) if dep != name
        ]
    plan.requirements = requirements

    # Fail before compiling anything if SAT selected a build-time cycle.
    visiting: Set[str] = set()
    visited: Set[str] = set()
    def visit(name: str) -> None:
        if name in visiting:
            raise ResolutionError(
                "source build dependency cycle: " + " -> ".join([*visiting, name])
                + " (move bootstrap-only edges into STAGE_2_REQUIRES)"
            )
        if name in visited:
            return
        visiting.add(name)
        for dependency in plan.direct_for(name):
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
    visit(root_name)
    return plan
