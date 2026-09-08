"""End-to-end BLFS-style source root and live ISO orchestration."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .chroot import ChrootMountState, mount_chroot_api, umount_chroot_api
from .live_iso import build_live_iso
from .source_bootstrap import build_and_install_sources


def read_profile(path: Path | str) -> tuple[str, ...]:
    profile = Path(path)
    if not profile.is_file():
        raise ValueError(f"package profile not found: {profile}")
    packages = tuple(
        line for raw in profile.read_text(encoding="utf-8").splitlines()
        if (line := raw.strip()) and not line.startswith("#")
    )
    if not packages:
        raise ValueError(f"package profile is empty: {profile}")
    return packages


def configure_live_root(root: Path, *, hostname: str = "lpm-live") -> list[Path]:
    files: list[Path] = []
    values = {
        "etc/hostname": hostname + "\n",
        "etc/hosts": "127.0.0.1 localhost\n127.0.1.1 " + hostname + "\n",
        "etc/os-release": (
            'NAME="LPM Linux"\nID=lpm\nPRETTY_NAME="LPM Linux Live"\n'
            'HOME_URL="https://github.com/BobTheZombie/LPM.Org"\n'
        ),
        "etc/NetworkManager/conf.d/10-lpm-live.conf": "[main]\nplugins=keyfile\n[device]\nwifi.scan-rand-mac-address=yes\n",
    }
    for relative, content in values.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        files.append(path)
    wants = root / "etc/systemd/system/multi-user.target.wants"
    wants.mkdir(parents=True, exist_ok=True)
    for unit in ("NetworkManager.service", "systemd-resolved.service"):
        link = wants / unit
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(Path("/usr/lib/systemd/system") / unit)
        files.append(link)
    resolv = root / "etc/resolv.conf"
    if resolv.exists() or resolv.is_symlink():
        resolv.unlink()
    resolv.symlink_to("../run/systemd/resolve/stub-resolv.conf")
    files.append(resolv)
    return files


def _kernel_version(root: Path) -> str:
    kernels = sorted((root / "boot").glob("vmlinuz-*"))
    if not kernels:
        raise RuntimeError("source build did not install a kernel below /boot")
    return kernels[-1].name.removeprefix("vmlinuz-")


def build_system_iso(args: Any) -> dict[str, object]:
    recipes = Path(args.lpmbuild_root).resolve()
    profile = Path(args.package_profile).resolve()
    root = Path(args.root).resolve()
    output = Path(args.output).resolve()
    artifacts = Path(args.artifact_dir or (root.parent / "lpm-packages")).resolve()
    packages = read_profile(profile)
    source_result = build_and_install_sources(
        recipes, root, artifacts, dry_run=bool(args.dry_run), include=packages,
        architecture=args.architecture,
    )
    result: dict[str, object] = {
        "root": str(root), "output": str(output), "architecture": args.architecture,
        "package_count": len(source_result["package_order"]),
        "package_order": source_result["package_order"], "dry_run": bool(args.dry_run),
    }
    if args.dry_run:
        return result
    if os.geteuid() != 0:
        raise PermissionError("systemiso requires root")
    configure_live_root(root, hostname=args.hostname)
    version = _kernel_version(root)
    mounts = ChrootMountState()
    try:
        mounts = mount_chroot_api(root, mounts)
        subprocess.run(
            ["chroot", str(root), "mkinitcpio", "-k", version, "-g", f"/boot/initramfs-{version}.img"],
            check=True,
        )
    finally:
        umount_chroot_api(root, mounts)
    result["iso"] = build_live_iso(
        root, output, volume_id=args.volume_id, architecture=args.architecture,
        dry_run=False, staging_root=args.iso_staging,
    )
    manifest = output.with_suffix(output.suffix + ".json")
    manifest.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["manifest"] = str(manifest)
    return result


__all__ = ["build_system_iso", "configure_live_root", "read_profile"]
