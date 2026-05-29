from __future__ import annotations

import json
import subprocess
import shutil
from pathlib import Path
from typing import Any

from lpm.bootstrap import _safe_target, generate_chroot_command, generate_lpm_root_install_command
from lpm.chroot import ChrootMountState, mount_chroot_api, umount_chroot_api


def _echo(message: str, *, verbose: bool = False) -> None:
    if verbose:
        print(message)


def _normalize_root(root: str | None) -> Path:
    return Path(root or "/")


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


def _run_root_install(target_root: Path, packages: list[str], *, dry_run: bool = False) -> dict[str, Any]:
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


def _bind_mount(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run(["mount", "--bind", str(source), str(target)], check=True)


def _umount_path(target: Path) -> None:
    subprocess.run(["umount", str(target)], check=True)


def _chroot_absolute(path: Path) -> str:
    return "/" + str(path).lstrip("/")


def _manifest_script_in_chroot(script: Path, source: Path, source_mount_rel: Path) -> str:
    try:
        rel = script.resolve().relative_to(source.resolve())
    except ValueError as exc:
        raise ValueError(f"manifest script is outside build source: {script}") from exc
    return _chroot_absolute(source_mount_rel / rel)


def _host_path_from_chroot_path(chroot_path: str, root: Path, cache_dir: Path, cache_mount_rel: Path) -> Path:
    path = Path(chroot_path)
    cache_mount = Path(_chroot_absolute(cache_mount_rel))
    try:
        rel = path.relative_to(cache_mount)
    except ValueError:
        return root / str(path).lstrip("/")
    return cache_dir / rel


def _run_chroot_lpmbuild(root: Path, script_in_chroot: str, outdir_in_chroot: str) -> dict[str, Any]:
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "from lpm import app as lpm_app\n"
        "blob, _elapsed, _size, splits = lpm_app.run_lpmbuild("
        "Path(sys.argv[1]), outdir=Path(sys.argv[2]), prompt_install=False, build_deps=True"
        ")\n"
        "print('LPM_BUILDCHROOT_RESULT=' + json.dumps({"
        "'blob': str(blob), 'splits': [str(path) for path, _meta in splits]"
        "}))\n"
    )
    cmd = generate_chroot_command(root, ["python3", "-c", code, script_in_chroot, outdir_in_chroot])
    proc = subprocess.run(cmd, check=False, text=True, capture_output=True)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="")
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=proc.stdout, stderr=proc.stderr)

    marker = "LPM_BUILDCHROOT_RESULT="
    for line in reversed((proc.stdout or "").splitlines()):
        if line.startswith(marker):
            return json.loads(line[len(marker):])
    raise RuntimeError("chroot lpmbuild did not report build artifacts")


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

    _echo(f"[buildgen] root={root} source={source} output={output_dir}", verbose=verbose)
    if args.dry_run:
        _echo("[buildgen] dry-run enabled; no filesystem changes", verbose=verbose)
        return 0

    from lpm import app as lpm_app

    scripts = sorted(source.glob("*/**/*.lpmbuild")) if source.is_dir() else []
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
            token = token.split(">=")[0].split("<=")[0].split("=")[0].split("<")[0].split(">")[0]
            if token:
                dep_names.add(token)
        meta_by_pkg[name] = {
            "name": name,
            "version": str(scal.get("VERSION") or scal.get("version") or ""),
            "script": str(script),
            "depends": sorted(dep_names),
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
        raise ValueError("Cycle detected in buildgen dependency graph. Cycle groups: " + ", ".join(remaining))

    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "source": str(source),
        "package_order": order,
        "packages": [meta_by_pkg[n] for n in order],
    }
    manifest_path = output_dir / "build-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
        tmp_args = type("BuildGenArgs", (), {
            "root": str(root), "source": str(source), "output_dir": str(output_dir), "dry_run": False, "verbose": verbose
        })()
        run_buildgen(tmp_args)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    packages = manifest.get("packages", []) or []
    missing = [p.get("script") for p in packages if not Path(str(p.get("script", ""))).exists()]
    if missing:
        raise ValueError("Missing .lpmbuild scripts for build targets: " + ", ".join(sorted(str(x) for x in missing)))

    root.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    staged_repo = output_dir / "repo"
    staged_repo.mkdir(parents=True, exist_ok=True)

    source_mount_rel = Path("tmp/lpm-buildchroot/source")
    cache_mount_rel = Path("tmp/lpm-buildchroot/cache")
    source_mount = root / source_mount_rel
    cache_mount = root / cache_mount_rel
    cache_in_chroot = _chroot_absolute(cache_mount_rel)

    mount_state = ChrootMountState(mounted=[])
    bind_mounts: list[Path] = []
    try:
        _bind_mount(source.resolve(), source_mount)
        bind_mounts.append(source_mount)
        _bind_mount(cache_dir.resolve(), cache_mount)
        bind_mounts.append(cache_mount)
        mount_state = mount_chroot_api(root, mount_state)

        for idx, pkg in enumerate(packages, start=1):
            name = str(pkg.get("name", ""))
            script = Path(str(pkg.get("script", "")))
            script_in_chroot = _manifest_script_in_chroot(script, source, source_mount_rel)
            print(f"[buildchroot {idx}/{len(packages)}] {name}")
            result = _run_chroot_lpmbuild(root, script_in_chroot, cache_in_chroot)
            built_paths = [str(result.get("blob", "")), *[str(p) for p in result.get("splits", [])]]
            for built in built_paths:
                if not built:
                    continue
                host_path = _host_path_from_chroot_path(built, root, cache_dir, cache_mount_rel)
                if not host_path.exists():
                    raise FileNotFoundError(f"built artifact not found outside chroot: {host_path}")
                shutil.copy2(host_path, staged_repo / host_path.name)
        return 0
    finally:
        try:
            umount_chroot_api(root, mount_state)
        finally:
            for mountpoint in reversed(bind_mounts):
                _umount_path(mountpoint)
