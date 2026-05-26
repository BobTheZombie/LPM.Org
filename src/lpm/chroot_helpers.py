from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Set, Tuple


def _echo(message: str, *, verbose: bool = False) -> None:
    if verbose:
        print(message)


def _normalize_root(root: str | None) -> Path:
    return Path(root or "/")


@dataclass(frozen=True)
class BuildManifestEntry:
    name: str
    script: str
    requires: Tuple[str, ...]
    build_requires: Tuple[str, ...]

    def as_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "script": self.script,
            "requires": list(self.requires),
            "build_requires": list(self.build_requires),
        }


def _parse_dep_names(requirements: Sequence[str]) -> Tuple[str, ...]:
    from . import app

    names: Set[str] = set()
    for raw in requirements:
        try:
            expr = app.parse_dep_expr(raw)
        except Exception:
            continue
        atoms = app.flatten_and(expr) if expr.kind == "and" else [expr]
        for atom_expr in atoms:
            if atom_expr.kind != "atom" or atom_expr.atom is None:
                continue
            dep_name = (atom_expr.atom.name or "").strip()
            if dep_name:
                names.add(dep_name)
    return tuple(sorted(names))


def _discover_lpmbuild_scripts(source: Path) -> List[Path]:
    if source.is_file():
        return [source]
    if not source.exists():
        return []
    return sorted(source.glob("**/*.lpmbuild"), key=lambda p: str(p.relative_to(source)))


def _build_manifest_entries(source: Path, scripts: Iterable[Path]) -> Dict[str, BuildManifestEntry]:
    from . import app

    entries: Dict[str, BuildManifestEntry] = {}
    for script in sorted(scripts, key=lambda p: str(p)):
        scalars, arrays, _maps = app._capture_lpmbuild_metadata(script)
        name = (scalars.get("NAME") or "").strip()
        if not name:
            continue
        rel_script = str(script.relative_to(source if source.is_dir() else script.parent.parent))
        requires = _parse_dep_names(list(arrays.get("REQUIRES", [])))
        build_requires = _parse_dep_names(list(arrays.get("BUILD_REQUIRES", [])))
        entries[name] = BuildManifestEntry(
            name=name,
            script=rel_script,
            requires=requires,
            build_requires=build_requires,
        )
    return entries


def _order_manifest(entries: Mapping[str, BuildManifestEntry]) -> Tuple[List[str], List[List[str]]]:
    nodes = sorted(entries.keys())
    edges: Dict[str, Set[str]] = {name: set() for name in nodes}
    indegree: Dict[str, int] = {name: 0 for name in nodes}
    for name, entry in entries.items():
        for dep in sorted(set(entry.requires) | set(entry.build_requires)):
            if dep not in entries:
                continue
            edges[dep].add(name)
            indegree[name] += 1

    ready = sorted([name for name, degree in indegree.items() if degree == 0])
    ordered: List[str] = []
    while ready:
        cur = ready.pop(0)
        ordered.append(cur)
        for nxt in sorted(edges[cur]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)
        ready.sort()

    if len(ordered) == len(nodes):
        return ordered, []

    unresolved = {name for name in nodes if indegree[name] > 0}
    cycle_groups = [[name] for name in sorted(unresolved)]
    return ordered, cycle_groups


def run_bootstrap_chroot(args: Any) -> int:
    root = _normalize_root(getattr(args, "root", None))
    cache_dir = Path(args.cache_dir)
    packages = list(getattr(args, "packages", []) or [])
    manifest = getattr(args, "manifest", None)
    verbose = bool(getattr(args, "verbose", False))

    if not packages and not manifest:
        raise ValueError("bootstrap-chroot requires --package or --manifest")

    _echo(f"[bootstrap-chroot] root={root} cache={cache_dir}", verbose=verbose)
    if args.dry_run:
        _echo("[bootstrap-chroot] dry-run enabled; no filesystem changes", verbose=verbose)
        return 0

    root.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return 0


def run_installroot(args: Any) -> int:
    root = _normalize_root(getattr(args, "root", None))
    cache_dir = Path(args.cache_dir)
    packages = list(getattr(args, "packages", []) or [])
    manifest = getattr(args, "manifest", None)
    verbose = bool(getattr(args, "verbose", False))

    if not packages and not manifest:
        raise ValueError("installroot requires --package or --manifest")

    _echo(f"[installroot] root={root} cache={cache_dir}", verbose=verbose)
    if args.dry_run:
        _echo("[installroot] dry-run enabled; no filesystem changes", verbose=verbose)
        return 0

    root.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return 0


def run_buildgen(args: Any) -> int:
    root = _normalize_root(getattr(args, "root", None))
    source = Path(args.source)
    output_dir = Path(args.output_dir)
    verbose = bool(getattr(args, "verbose", False))

    _echo(f"[buildgen] root={root} source={source} output={output_dir}", verbose=verbose)
    if args.dry_run:
        _echo("[buildgen] dry-run enabled; no filesystem changes", verbose=verbose)
        return 0

    scripts = _discover_lpmbuild_scripts(source)
    if not scripts:
        raise ValueError(f"No .lpmbuild scripts found under {source}")

    entries = _build_manifest_entries(source, scripts)
    ordered, cycle_groups = _order_manifest(entries)
    if cycle_groups:
        groups = "; ".join(", ".join(group) for group in cycle_groups)
        raise ValueError(
            "Cycle detected in buildgen dependency graph. "
            f"Cycle groups: {groups}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "build-manifest.json"
    payload = {
        "source": str(source),
        "packages": [entries[name].as_dict() for name in ordered],
    }
    manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


def run_buildchroot(args: Any) -> int:
    root = _normalize_root(getattr(args, "root", None))
    source = Path(args.source)
    cache_dir = Path(args.cache_dir)
    output_dir = Path(args.output_dir)
    verbose = bool(getattr(args, "verbose", False))

    _echo(
        f"[buildchroot] root={root} source={source} cache={cache_dir} output={output_dir}",
        verbose=verbose,
    )
    if args.dry_run:
        _echo("[buildchroot] dry-run enabled; no filesystem changes", verbose=verbose)
        return 0

    manifest_path = output_dir / "build-manifest.json"
    if not manifest_path.exists():
        raise ValueError(f"build manifest not found: {manifest_path}")

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    packages = data.get("packages") if isinstance(data, dict) else None
    if not isinstance(packages, list):
        raise ValueError(f"invalid build manifest: {manifest_path}")

    from . import app

    source_root = source if source.is_dir() else source.parent

    root.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    for idx, pkg in enumerate(packages, start=1):
        if not isinstance(pkg, dict):
            continue
        name = str(pkg.get("name") or "")
        rel_script = str(pkg.get("script") or "")
        script = source_root / rel_script
        if not script.exists():
            raise ValueError(f"Missing .lpmbuild scripts for buildchroot targets: {name} ({script})")
        _echo(f"[buildchroot {idx}/{len(packages)}] {name}", verbose=verbose)
        app.run_lpmbuild(
            script,
            outdir=cache_dir,
            prompt_install=False,
            build_deps=True,
            fetcher=app.fetch_lpmbuild,
        )
    return 0
