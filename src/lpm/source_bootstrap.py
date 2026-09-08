"""Build and install a target root entirely from local ``.lpmbuild`` files."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class SourcePackage:
    name: str
    script: Path
    dependencies: tuple[str, ...]
    provides: tuple[str, ...]


def discover_source_packages(root: Path | str) -> dict[str, SourcePackage]:
    from . import app

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
    from . import app

    providers: dict[str, set[str]] = {name: {name} for name in packages}
    for package in packages.values():
        for raw in package.provides:
            try:
                expression = app.parse_dep_expr(raw)
                if expression.kind == "atom" and expression.atom:
                    providers.setdefault(expression.atom.name, set()).add(package.name)
            except Exception:
                providers.setdefault(str(raw).split()[0], set()).add(package.name)

    dependencies: dict[str, set[str]] = {name: set() for name in packages}
    for package in packages.values():
        for raw in package.dependencies:
            expression = app.parse_dep_expr(raw)
            alternatives = app.flatten_or(expression) if expression.kind == "or" else (
                app.flatten_and(expression) if expression.kind == "and" else [expression]
            )
            matches: set[str] = set()
            for part in alternatives:
                if part.kind == "atom" and part.atom:
                    matches.update(providers.get(part.atom.name, set()))
            matches.discard(package.name)
            if len(matches) == 1:
                dependencies[package.name].update(matches)
            elif len(matches) > 1:
                raise ValueError(f"ambiguous source provider for {raw!r} required by {package.name}: {', '.join(sorted(matches))}")

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
        cycle = sorted(name for name, count in indegree.items() if count)
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
    build: Callable[..., Any] | None = None,
    install: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Build every local recipe once and install artifacts dependency-first."""
    from . import app

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
            order_probe = source_build_order(packages)
            by_name = {item.name: item for item in order_probe}
            for name in tuple(selected):
                package = by_name[name]
                for raw in package.dependencies:
                    token = str(raw).split()[0]
                    if token in packages and token not in selected:
                        selected.add(token)
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
        artifact, _duration, _dependency_count, splits = build_fn(
            package.script, outdir=out, prompt_install=False, build_deps=False
        )
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
