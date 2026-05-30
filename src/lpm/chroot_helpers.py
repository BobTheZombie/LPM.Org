from __future__ import annotations

import json
import subprocess
import shutil
from pathlib import Path
from typing import Any

from lpm.bootstrap import (
    _safe_target,
    generate_chroot_command,
    generate_lpm_root_install_command,
)
from lpm.chroot import ChrootMountState, mount_chroot_api, umount_chroot_api


def _echo(message: str, *, verbose: bool = False) -> None:
    if verbose:
        print(message)


def _normalize_root(root: str | None) -> Path:
    return Path(root or "/")


def _stable_path(path: str | Path) -> str:
    """Serialize paths consistently for deterministic JSON manifests."""
    return Path(path).as_posix()


def _read_manifest_packages(manifest: str | None) -> list[str]:
    if not manifest:
        return []
    path = Path(manifest)
    if not path.exists():
        raise ValueError(f"manifest not found: {path}")
    packages: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        packages.append(line)
    return packages


def _collect_packages(args: Any) -> list[str]:
    cli_packages = list(getattr(args, "packages", []) or [])
    manifest_packages = _read_manifest_packages(getattr(args, "manifest", None))
    combined = cli_packages + manifest_packages
    if not combined:
        raise ValueError("installroot requires --package or --manifest")
    return combined


def _run_root_install(
    target_root: Path, packages: list[str], *, dry_run: bool = False
) -> dict[str, Any]:
    cmd = generate_lpm_root_install_command(target_root, packages)
    result: dict[str, Any] = {
        "target_root": str(target_root),
        "packages_requested": packages,
        "command": cmd,
        "dry_run": dry_run,
        "installed": [],
        "failed": [],
        "returncode": 0,
    }
    if dry_run:
        return result

    proc = subprocess.run(cmd, check=False)
    result["returncode"] = proc.returncode
    if proc.returncode == 0:
        result["installed"] = packages
    else:
        result["failed"] = packages
    return result


def _root_relative(path: str | Path) -> Path:
    path = Path(path)
    if path.is_absolute():
        return Path(*path.parts[1:])
    return path


def _chroot_path(path: str | Path) -> str:
    rel = _root_relative(path)
    return "/" + rel.as_posix()


def _run_root_install_local(
    target_root: Path, artifacts: list[Path], *, dry_run: bool = False
) -> dict[str, Any]:
    files = [str(path) for path in artifacts]
    cmd = ["lpm", "installpkg", *files, "--root", str(target_root)]
    result: dict[str, Any] = {
        "target_root": str(target_root),
        "artifacts_requested": files,
        "command": cmd,
        "dry_run": dry_run,
        "installed": [],
        "failed": [],
        "returncode": 0,
    }
    if dry_run:
        return result

    proc = subprocess.run(cmd, check=False)
    result["returncode"] = proc.returncode
    if proc.returncode == 0:
        result["installed"] = files
    else:
        result["failed"] = files
    return result


def _stage_build_inputs(root: Path, packages: list[dict[str, Any]]) -> dict[str, Path]:
    staged: dict[str, Path] = {}
    inputs_root = root / "var/lib/lpm/buildchroot/inputs"
    inputs_root.mkdir(parents=True, exist_ok=True)
    for idx, pkg in enumerate(packages, start=1):
        script = Path(str(pkg.get("script", "")))
        name = str(pkg.get("name") or script.stem or f"pkg-{idx}")
        dest_dir = inputs_root / f"{idx:04d}-{name}"
        if script.parent.exists():
            shutil.copytree(script.parent, dest_dir, dirs_exist_ok=True)
        else:
            dest_dir.mkdir(parents=True, exist_ok=True)
        dest_script = dest_dir / script.name
        if not dest_script.exists() and script.exists():
            shutil.copy2(script, dest_script)
        staged[name] = dest_script
    return staged


def _run_chroot_build(root: Path, script: Path, outdir: Path) -> int:
    cmd = generate_chroot_command(
        root,
        [
            "lpm",
            "buildpkg",
            _chroot_path(script.relative_to(root)),
            "--outdir",
            _chroot_path(outdir.relative_to(root)),
            "--install-default",
            "n",
        ],
    )
    proc = subprocess.run(cmd, check=False)
    return proc.returncode


def _collect_chroot_artifacts(
    chroot_outdir: Path, before: set[Path], pkg: dict[str, Any]
) -> list[Path]:
    current = set(chroot_outdir.glob("*.zst"))
    new_artifacts = sorted(current - before)
    if new_artifacts:
        return new_artifacts

    planned = []
    for key in ("planned_artifacts",):
        for raw in pkg.get(key) or []:
            candidate = chroot_outdir / Path(str(raw)).name
            if candidate.exists():
                planned.append(candidate)
    raw_single = pkg.get("planned_artifact")
    if raw_single:
        candidate = chroot_outdir / Path(str(raw_single)).name
        if candidate.exists():
            planned.append(candidate)
    return sorted(set(planned))


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
        _echo(
            "[bootstrap-chroot] dry-run enabled; no filesystem changes", verbose=verbose
        )
        return 0

    root.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    return 0


def run_installroot(args: Any) -> int:
    root = _normalize_root(getattr(args, "root", None))
    cache_dir = Path(args.cache_dir)
    verbose = bool(getattr(args, "verbose", False))
    mount_api = bool(getattr(args, "mount_api", False))

    _safe_target(root)
    packages = _collect_packages(args)

    _echo(f"[installroot] root={root} cache={cache_dir}", verbose=verbose)
    mount_state = ChrootMountState(mounted=[])
    if args.dry_run:
        result = _run_root_install(root, packages, dry_run=True)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    root.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    try:
        if mount_api:
            mount_state = mount_chroot_api(root, mount_state)
        result = _run_root_install(root, packages)
        print(json.dumps(result, indent=2, sort_keys=True))
        return int(result["returncode"])
    finally:
        if mount_api:
            umount_chroot_api(root, mount_state)


def run_buildgen(args: Any) -> int:
    root = _normalize_root(getattr(args, "root", None))
    source = Path(args.source)
    output_dir = Path(args.output_dir)
    verbose = bool(getattr(args, "verbose", False))

    _echo(
        f"[buildgen] root={root} source={source} output={output_dir}", verbose=verbose
    )
    if args.dry_run:
        _echo("[buildgen] dry-run enabled; no filesystem changes", verbose=verbose)
        return 0

    from lpm import app as lpm_app

    if source.is_file() and source.suffix == ".lpmbuild":
        scripts = [source]
    elif source.is_dir():
        scripts = sorted(source.rglob("*.lpmbuild"))
    else:
        raise ValueError(f"no .lpmbuild scripts found under: {source}")

    if not scripts:
        raise ValueError(f"no .lpmbuild scripts found under: {source}")

    meta_by_pkg: dict[str, dict[str, Any]] = {}
    deps_by_pkg: dict[str, set[str]] = {}
    for script in scripts:
        scal, arr, _maps = lpm_app._capture_lpmbuild_metadata(script)
        name = str(scal.get("NAME") or scal.get("name") or "").strip()
        if not name:
            continue
        dep_fields = []
        for key in ("REQUIRES", "requires", "BUILD_REQUIRES", "build_requires"):
            dep_fields.extend([str(x) for x in (arr.get(key) or []) if x])
        dep_names: set[str] = set()
        for raw in dep_fields:
            try:
                dep_names.add(lpm_app.parse_dep_expr(raw).name)
                continue
            except Exception:
                pass
            token = str(raw).strip().split()[0] if str(raw).strip() else ""
            token = (
                token.split(">=")[0]
                .split("<=")[0]
                .split("=")[0]
                .split("<")[0]
                .split(">")[0]
            )
            if token:
                dep_names.add(token)
        version = str(scal.get("VERSION") or scal.get("version") or "")
        release = str(scal.get("RELEASE") or scal.get("release") or "1")
        arch = str(scal.get("ARCH") or scal.get("arch") or "noarch")
        repo_dir = output_dir / "repo"
        planned_artifact = repo_dir / f"{name}-{version}-{release}.{arch}.zst"
        meta_by_pkg[name] = {
            "name": name,
            "version": version,
            "release": release,
            "arch": arch,
            "script": _stable_path(script),
            "depends": sorted(dep_names),
            "build_output_dir": _stable_path(output_dir / "build" / name),
            "repo_dir": _stable_path(repo_dir),
            "planned_artifact": _stable_path(planned_artifact),
            "planned_artifacts": [_stable_path(planned_artifact)],
        }
        deps_by_pkg[name] = set(dep_names)

    known = set(meta_by_pkg)
    for name, deps in deps_by_pkg.items():
        deps.intersection_update(known)
        meta_by_pkg[name]["depends"] = sorted(deps)

    indeg = {k: 0 for k in known}
    rev: dict[str, set[str]] = {k: set() for k in known}
    for pkg in sorted(known):
        for dep in sorted(deps_by_pkg[pkg]):
            indeg[pkg] += 1
            rev[dep].add(pkg)
    queue = sorted([k for k, d in indeg.items() if d == 0])
    order: list[str] = []
    while queue:
        cur = queue.pop(0)
        order.append(cur)
        for nxt in sorted(rev[cur]):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
                queue.sort()
    if len(order) != len(known):
        remaining = sorted([k for k, d in indeg.items() if d > 0])
        raise ValueError(
            "Cycle detected in buildgen dependency graph. Cycle groups: "
            + ", ".join(remaining)
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = output_dir / "repo"
    bootstrap_packages = sorted(
        str(pkg)
        for pkg in (
            getattr(args, "bootstrap_packages", None)
            or getattr(args, "packages", [])
            or []
        )
    )
    manifest = {
        "root": _stable_path(root),
        "source": _stable_path(source),
        "output_dir": _stable_path(output_dir),
        "repo_dir": _stable_path(repo_dir),
        "bootstrap_packages": bootstrap_packages,
        "package_order": order,
        "packages": [meta_by_pkg[n] for n in order],
        "chroot_setup": {
            "root": _stable_path(root),
            "output_dir": _stable_path(output_dir),
            "repo_dir": _stable_path(repo_dir),
            "bootstrap_packages": bootstrap_packages,
        },
    }
    manifest_path = output_dir / "build-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(str(manifest_path))
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
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        tmp_args = type(
            "BuildGenArgs",
            (),
            {
                "root": str(root),
                "source": str(source),
                "output_dir": str(output_dir),
                "dry_run": False,
                "verbose": verbose,
            },
        )()
        run_buildgen(tmp_args)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    setup = (
        manifest.get("chroot_setup", {})
        if isinstance(manifest.get("chroot_setup", {}), dict)
        else {}
    )
    root = Path(str(manifest.get("root") or setup.get("root") or root))
    output_dir = Path(
        str(manifest.get("output_dir") or setup.get("output_dir") or output_dir)
    )
    # The host-side staged repository is intentionally derived from the active
    # output directory so locally built artifacts are installed from a stable,
    # predictable repo regardless of stale or externally edited manifest data.
    staged_repo = output_dir / "repo"
    bootstrap_packages = [
        str(pkg)
        for pkg in (
            manifest.get("bootstrap_packages") or setup.get("bootstrap_packages") or []
        )
    ]

    packages = manifest.get("packages", []) or []
    missing = [
        p.get("script") for p in packages if not Path(str(p.get("script", ""))).exists()
    ]
    if missing:
        raise ValueError(
            "Missing .lpmbuild scripts for build targets: "
            + ", ".join(sorted(str(x) for x in missing))
        )

    root.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    staged_repo.mkdir(parents=True, exist_ok=True)

    chroot_outdir = root / "var/cache/lpm/buildchroot"
    chroot_outdir.mkdir(parents=True, exist_ok=True)
    staged_scripts = _stage_build_inputs(root, packages)

    mount_state = ChrootMountState(mounted=[])
    try:
        mount_state = mount_chroot_api(root, mount_state)
        if bootstrap_packages:
            bootstrap_result = _run_root_install(root, bootstrap_packages)
            print(json.dumps(bootstrap_result, indent=2, sort_keys=True))
            bootstrap_rc = int(bootstrap_result.get("returncode", 0))
            if bootstrap_rc != 0:
                return bootstrap_rc

        built_artifacts: list[Path] = []
        for idx, pkg in enumerate(packages, start=1):
            name = str(pkg.get("name", ""))
            staged_script = staged_scripts.get(name)
            if staged_script is None:
                staged_script = next(iter(staged_scripts.values()))
            print(f"[buildchroot {idx}/{len(packages)}] {name}")
            before = set(chroot_outdir.glob("*.zst"))
            build_rc = _run_chroot_build(root, staged_script, chroot_outdir)
            if build_rc != 0:
                return build_rc
            artifacts = _collect_chroot_artifacts(chroot_outdir, before, pkg)
            for blob in artifacts:
                dest = staged_repo / blob.name
                shutil.copy2(blob, dest)
                built_artifacts.append(dest)

        install_result = _run_root_install_local(root, built_artifacts)
        print(json.dumps(install_result, indent=2, sort_keys=True))
        return int(install_result.get("returncode", 0))
    finally:
        umount_chroot_api(root, mount_state)
