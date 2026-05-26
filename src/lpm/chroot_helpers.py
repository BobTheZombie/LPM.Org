from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from lpm.bootstrap import _safe_target, generate_lpm_root_install_command
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

    output_dir.mkdir(parents=True, exist_ok=True)
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

    root.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    return 0
