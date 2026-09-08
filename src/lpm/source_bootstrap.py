"""Build and install a target root entirely from local ``.lpmbuild`` files."""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class SourcePackage:
    name: str
    script: Path
    dependencies: tuple[str, ...]
    provides: tuple[str, ...]


def discover_source_packages(root: Path | str) -> dict[str, SourcePackage]:
    app = import_module(".app", __package__)

    result: dict[str, SourcePackage] = {}
    for script in sorted(Path(root).rglob("*.lpmbuild")):
        scalars, arrays, mappings = app._capture_lpmbuild_metadata(script)
        name = str(scalars.get("NAME") or scalars.get("name") or "").strip()
        if not name:
            raise ValueError(f"{script}: lpmbuild is missing NAME")
        if name in result:
            raise ValueError(f"duplicate lpmbuild package name {name!r}: {result[name].script} and {script}")
        deps = arrays.get("REQUIRES", []) + arrays.get("BUILD_REQUIRES", [])
        provides = list(arrays.get("PROVIDES", []))
        for values in mappings.get("PROVIDES", {}).values():
            provides.extend(values)
        result[name] = SourcePackage(name, script.resolve(), tuple(deps), tuple(provides))
    if not result:
        raise ValueError(f"no .lpmbuild files found under {root}")
    return result


def source_build_order(packages: dict[str, SourcePackage]) -> list[SourcePackage]:
    app = import_module(".app", __package__)

    providers: dict[str, set[str]] = {name: {name} for name in packages}
    for package in packages.values():
        for raw in package.provides:
            try:
                expression = app.parse_dep_expr(raw)
                if expression.kind == "atom" and expression.atom:
                    providers.setdefault(expression.atom.name, set()).add(package.name)
            except Exception:
                providers.setdefault(str(raw).split()[0], set()).add(package.name)

    def resolve_expression(expression: Any, package: SourcePackage, raw: str) -> set[str]:
        if expression.kind == "atom" and expression.atom:
            capability = expression.atom.name
            matches = {capability} if capability in packages else set(providers.get(capability, set()))
            matches.discard(package.name)
            if len(matches) > 1:
                raise ValueError(
                    f"ambiguous source provider for {raw!r} required by {package.name}: "
                    + ", ".join(sorted(matches))
                )
            return matches
        if expression.kind == "and":
            result: set[str] = set()
            for part in app.flatten_and(expression):
                result.update(resolve_expression(part, package, raw))
            return result
        if expression.kind == "or":
            # Alternatives are choices, not a requirement to build every
            # provider. Prefer the first satisfiable local alternative.
            for part in app.flatten_or(expression):
                matches = resolve_expression(part, package, raw)
                if matches:
                    return matches
            return set()
        return set()

    dependencies: dict[str, set[str]] = {name: set() for name in packages}
    for package in packages.values():
        for raw in package.dependencies:
            dependencies[package.name].update(resolve_expression(app.parse_dep_expr(raw), package, raw))

    indegree = {name: len(deps) for name, deps in dependencies.items()}
    reverse: dict[str, set[str]] = {name: set() for name in packages}
    for name, deps in dependencies.items():
        for dependency in deps:
            reverse[dependency].add(name)
    ready = sorted(name for name, count in indegree.items() if count == 0)
    order: list[str] = []
    while ready:
        name = ready.pop(0)
        order.append(name)
        for dependent in sorted(reverse[name]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort()
    if len(order) != len(packages):
        unresolved = {name for name, count in indegree.items() if count}
        visiting: list[str] = []
        active: set[str] = set()
        finished: set[str] = set()

        def find_cycle(name: str) -> list[str] | None:
            if name in active:
                start = visiting.index(name)
                return [*visiting[start:], name]
            if name in finished:
                return None
            active.add(name)
            visiting.append(name)
            for dependency in sorted(dependencies[name] & unresolved):
                if cycle := find_cycle(dependency):
                    return cycle
            visiting.pop()
            active.remove(name)
            finished.add(name)
            return None

        cycle = next(
            (found for name in sorted(unresolved) if (found := find_cycle(name))),
            sorted(unresolved),
        )
        raise ValueError("source dependency cycle: " + " -> ".join(cycle))
    return [packages[name] for name in order]


def build_and_install_sources(
    source_root: Path | str,
    target: Path | str,
    output_dir: Path | str,
    *,
    dry_run: bool = False,
    include: tuple[str, ...] = (),
    exclude: tuple[str, ...] = (),
    architecture: str | None = None,
    build: Callable[..., Any] | None = None,
    install: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Build every local recipe once and install artifacts dependency-first."""
    app = import_module(".app", __package__)

    packages = discover_source_packages(source_root)
    unknown = (set(include) | set(exclude)) - set(packages)
    if unknown:
        raise ValueError("unknown source package(s): " + ", ".join(sorted(unknown)))
    if include:
        selected = set(include)
        # Include locally available dependencies transitively.
        changed = True
        while changed:
            changed = False
            for name in tuple(selected):
                package = packages[name]
                for raw in package.dependencies:
                    expression = app.parse_dep_expr(raw)
                    candidates = app.flatten_or(expression) if expression.kind == "or" else (
                        app.flatten_and(expression) if expression.kind == "and" else [expression]
                    )
                    available = [
                        part.atom.name for part in candidates
                        if part.kind == "atom" and part.atom and part.atom.name in packages
                    ]
                    if expression.kind == "or":
                        available = available[:1]
                    for dependency in available:
                        if dependency in selected:
                            continue
                        selected.add(dependency)
                        changed = True
        packages = {name: package for name, package in packages.items() if name in selected}
    if exclude:
        packages = {name: package for name, package in packages.items() if name not in set(exclude)}
    if not packages:
        raise ValueError("source package selection is empty")
    order = source_build_order(packages)
    result: dict[str, Any] = {"package_order": [item.name for item in order], "artifacts": [], "installed": []}
    if dry_run:
        return result
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    build_fn = build or app.run_lpmbuild
    install_fn = install or app.installpkg
    artifacts: list[Path] = []
    for package in order:
        build_kwargs: dict[str, Any] = {"outdir": out, "prompt_install": False, "build_deps": False}
        if architecture:
            march = "x86-64-v2" if architecture == "x86_64-v2" else "x86-64"
            build_kwargs["cpu_overrides"] = app.CpuOverrides(
                arch="x86_64", march=march, mtune="generic"
            )
        artifact, _duration, _dependency_count, splits = build_fn(package.script, **build_kwargs)
        current = [Path(artifact), *(Path(path) for path, _meta in splits)]
        for path in current:
            if not path.is_file():
                raise RuntimeError(f"build did not produce expected artifact: {path}")
        artifacts.extend(current)
        result["artifacts"].extend(str(path) for path in current)
    for artifact in artifacts:
        install_fn(artifact, root=Path(target), dry_run=False, verify=False, force=False, explicit=True)
        result["installed"].append(str(artifact))
    manifest = Path(target) / "var/lib/lpm/source-bootstrap.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


__all__ = ["SourcePackage", "build_and_install_sources", "discover_source_packages", "source_build_order"]
