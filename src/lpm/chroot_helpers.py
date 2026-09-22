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



# Packages that make a source-built target capable of building the rest of the
# graph itself.  Only names present in the manifest are selected.  Their full
# dependency closure is included automatically.
DEFAULT_STAGE0_PACKAGES = (
    "filesystem",
    "glibc",
    "binutils",
    "gcc",
    "bash",
    "coreutils",
    "make",
    "python",
    "pkgconf",
    "sed",
    "grep",
    "gawk",
    "findutils",
    "diffutils",
    "tar",
    "gzip",
    "bzip2",
    "xz",
    "patch",
    "file",
    "util-linux",
    "lpm-filesystem",
    "lpm",
)


def _stage0_package_names(
    packages: list[dict[str, Any]], requested: list[str] | None = None
) -> set[str]:
    by_name = {str(pkg.get("name", "")): pkg for pkg in packages}
    targets = list(requested or [])
    if not targets:
        targets = [name for name in DEFAULT_STAGE0_PACKAGES if name in by_name]

    missing = sorted(name for name in targets if name not in by_name)
    if missing:
        raise ValueError(
            "stage-0 package(s) are not present in the build manifest: "
            + ", ".join(missing)
        )
    if "lpm" not in by_name:
        raise ValueError(
            "true stage-0 bootstrap requires an lpm.lpmbuild in --source"
        )
    if "lpm" not in targets:
        targets.append("lpm")

    selected: set[str] = set()

    def add_closure(name: str) -> None:
        if name in selected:
            return
        selected.add(name)
        for dep in by_name[name].get("depends", []) or []:
            dep_name = str(dep)
            if dep_name in by_name:
                add_closure(dep_name)

    for target in targets:
        add_closure(target)
    return selected


def _run_host_build(script: Path, outdir: Path) -> int:
    cmd = [
        "lpm",
        "buildpkg",
        str(script),
        "--outdir",
        str(outdir),
        "--install-default",
        "n",
        "--no-deps",
    ]
    return subprocess.run(cmd, check=False).returncode


def _target_stage0_ready(root: Path) -> bool:
    lpm = root / "usr/bin/lpm"
    shells = (root / "usr/bin/bash", root / "bin/bash", root / "bin/sh")
    return lpm.exists() and any(shell.exists() for shell in shells)


def _run_stage0(
    root: Path,
    packages: list[dict[str, Any]],
    staged_repo: Path,
    stage0_names: set[str],
    *,
    verbose: bool = False,
) -> tuple[int, list[Path], set[str]]:
    """Build the seed graph on the host and install it into an empty target."""
    built_artifacts: list[Path] = []
    completed: set[str] = set()

    for idx, pkg in enumerate(packages, start=1):
        name = str(pkg.get("name", ""))
        if name not in stage0_names:
            continue
        script = Path(str(pkg.get("script", "")))
        print(f"[stage0 {len(completed) + 1}/{len(stage0_names)}] {name}")
        before = set(staged_repo.glob("*.zst"))
        rc = _run_host_build(script, staged_repo)
        if rc != 0:
            return rc, built_artifacts, completed

        artifacts = _collect_chroot_artifacts(staged_repo, before, pkg)
        if not artifacts:
            raise RuntimeError(f"stage-0 build produced no package artifact for {name}")

        result = _run_root_install_local(root, artifacts)
        rc = int(result.get("returncode", 0))
        if rc != 0:
            return rc, built_artifacts, completed

        built_artifacts.extend(artifacts)
        completed.add(name)
        _echo(f"[stage0] installed {name} into {root}", verbose=verbose)

    if completed != stage0_names:
        missing = sorted(stage0_names - completed)
        raise RuntimeError("stage-0 did not build required packages: " + ", ".join(missing))
    if not _target_stage0_ready(root):
        raise RuntimeError(
            "stage-0 completed but target is not chroot-ready; "
            "expected /usr/bin/lpm and a usable shell"
        )
    return 0, built_artifacts, completed


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

    def _metadata_name(raw: Any) -> str:
        try:
            expr = lpm_app.parse_dep_expr(str(raw))
            if getattr(expr, "kind", None) == "atom" and getattr(expr, "atom", None):
                parsed_name = str(expr.atom.name).strip()
                if parsed_name:
                    return parsed_name
        except Exception:
            pass
        token = str(raw).strip().split()[0] if str(raw).strip() else ""
        token = (
            token.split(">=")[0]
            .split("<=")[0]
            .split("==")[0]
            .split("=")[0]
            .split("<")[0]
            .split(">")[0]
        )
        return token.strip()

    meta_by_pkg: dict[str, dict[str, Any]] = {}
    deps_by_pkg: dict[str, set[str]] = {}
    raw_provides_by_pkg: dict[str, set[str]] = {}
    mapped_provides_by_pkg: dict[str, set[str]] = {}
    for script in scripts:
        scal, arr, maps = lpm_app._capture_lpmbuild_metadata(script)
        name = str(scal.get("NAME") or scal.get("name") or "").strip()
        if not name:
            continue
        dep_fields = []
        for key in ("REQUIRES", "requires", "BUILD_REQUIRES", "build_requires"):
            dep_fields.extend([str(x) for x in (arr.get(key) or []) if x])
        dep_names: set[str] = set()
        for raw in dep_fields:
            token = _metadata_name(raw)
            if token:
                dep_names.add(token)

        provides: set[str] = set()
        for raw in arr.get("PROVIDES") or arr.get("provides") or []:
            token = _metadata_name(raw)
            if token:
                provides.add(token)
        raw_provides_by_pkg[name] = set(provides)

        meta_provides = maps.get("META_PROVIDES") or maps.get("meta_provides") or {}
        for provider, values in sorted(meta_provides.items()):
            provider_name = str(provider).strip()
            if not provider_name:
                continue
            provider_caps = mapped_provides_by_pkg.setdefault(provider_name, set())
            for raw in values or []:
                token = _metadata_name(raw)
                if token:
                    provider_caps.add(token)
                    if provider_name == name:
                        provides.add(token)

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
            "provides": sorted(provides),
            "build_output_dir": _stable_path(output_dir / "build" / name),
            "repo_dir": _stable_path(repo_dir),
            "planned_artifact": _stable_path(planned_artifact),
            "planned_artifacts": [_stable_path(planned_artifact)],
        }
        deps_by_pkg[name] = set(dep_names)

    known = set(meta_by_pkg)
    provider_index: dict[str, list[str]] = {}
    for name in sorted(known):
        provides = set(raw_provides_by_pkg.get(name, set()))
        provides.update(mapped_provides_by_pkg.get(name, set()))
        meta_by_pkg[name]["provides"] = sorted(provides)
        for capability in sorted(provides):
            provider_index.setdefault(capability, []).append(name)
    provider_index = {
        capability: sorted(set(providers))
        for capability, providers in sorted(provider_index.items())
    }

    for name, deps in deps_by_pkg.items():
        normalized_deps: set[str] = set()
        for dep in sorted(deps):
            if dep in known:
                normalized_deps.add(dep)
                continue
            providers = provider_index.get(dep, [])
            if len(providers) == 1:
                normalized_deps.add(providers[0])
            elif len(providers) > 1:
                raise ValueError(
                    f"ambiguous provider for dependency {dep!r}: "
                    + ", ".join(providers)
                )
        deps_by_pkg[name] = normalized_deps
        meta_by_pkg[name]["depends"] = sorted(normalized_deps)

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

    # A genuinely empty target cannot execute `lpm buildpkg`.  Seed a
    # minimal source-built toolchain from the host first, installing every
    # resulting package into the target.  Once LPM + a shell exist there, the
    # remainder of the graph is built from inside the target itself.
    stage0_completed: set[str] = set()
    force_stage0 = bool(getattr(args, "stage0", False))
    stage0_requested = list(getattr(args, "stage0_packages", []) or [])
    if force_stage0 or not _target_stage0_ready(root):
        stage0_names = _stage0_package_names(packages, stage0_requested)
        _echo(
            "[stage0] seed packages: " + ", ".join(
                name for name in manifest.get("package_order", []) if name in stage0_names
            ),
            verbose=verbose,
        )
        stage0_rc, _stage0_artifacts, stage0_completed = _run_stage0(
            root,
            packages,
            staged_repo,
            stage0_names,
            verbose=verbose,
        )
        if stage0_rc != 0:
            return stage0_rc

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
        remaining = [pkg for pkg in packages if str(pkg.get("name", "")) not in stage0_completed]
        for idx, pkg in enumerate(remaining, start=1):
            name = str(pkg.get("name", ""))
            staged_script = staged_scripts.get(name)
            if staged_script is None:
                raise RuntimeError(f"no staged .lpmbuild found for {name}")
            print(f"[buildchroot {idx}/{len(remaining)}] {name}")
            before = set(chroot_outdir.glob("*.zst"))
            build_rc = _run_chroot_build(root, staged_script, chroot_outdir)
            if build_rc != 0:
                return build_rc
            artifacts = _collect_chroot_artifacts(chroot_outdir, before, pkg)
            if not artifacts:
                raise RuntimeError(f"chroot build produced no package artifact for {name}")
            for blob in artifacts:
                dest = staged_repo / blob.name
                shutil.copy2(blob, dest)
                built_artifacts.append(dest)

            # Install each package immediately.  Later recipes can therefore
            # consume BUILD_REQUIRES produced earlier in the topological order.
            install_result = _run_root_install_local(root, [staged_repo / blob.name for blob in artifacts])
            install_rc = int(install_result.get("returncode", 0))
            if install_rc != 0:
                return install_rc

        return 0
    finally:
        umount_chroot_api(root, mount_state)
