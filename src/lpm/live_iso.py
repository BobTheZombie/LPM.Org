"""Build a GRUB bootable live ISO from an LPM target root."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


SUPPORTED_ARCHITECTURES = {"x86_64", "x86_64-v2"}


@dataclass(frozen=True)
class LiveISOPlan:
    source_root: Path
    output: Path
    volume_id: str
    architecture: str
    kernel: Path
    initramfs: Path
    squashfs: Path
    iso_root: Path
    commands: tuple[tuple[str, ...], ...]


def _find_boot_file(root: Path, explicit: str | None, patterns: Sequence[str], label: str) -> Path:
    if explicit:
        candidate = Path(explicit)
        if not candidate.is_absolute():
            candidate = root / candidate
        if candidate.is_file():
            return candidate.resolve()
        raise ValueError(f"{label} not found: {candidate}")
    flattened = sorted(
        path
        for pattern in patterns
        for path in (root / "boot").glob(pattern)
        if path.is_file()
    )
    if not flattened:
        raise ValueError(f"no {label} found below {root / 'boot'}")
    return flattened[-1].resolve()


def _validate_volume_id(value: str) -> str:
    volume_id = value.strip().upper()
    if not re.fullmatch(r"[A-Z0-9_]{1,32}", volume_id):
        raise ValueError("volume ID must contain 1-32 uppercase letters, digits, or underscores")
    return volume_id


def plan_live_iso(
    source_root: Path | str,
    output: Path | str,
    *,
    volume_id: str = "LPM_LIVE",
    architecture: str = "x86_64-v2",
    kernel: str | None = None,
    initramfs: str | None = None,
    staging_root: Path | str,
) -> LiveISOPlan:
    root = Path(source_root).resolve()
    if not root.is_dir():
        raise ValueError(f"source root does not exist: {root}")
    if architecture not in SUPPORTED_ARCHITECTURES:
        raise ValueError(f"unsupported live ISO architecture: {architecture}")
    stage = Path(staging_root).resolve()
    iso_root = stage / "iso"
    squashfs = iso_root / "live/rootfs.squashfs"
    kernel_path = _find_boot_file(root, kernel, ("vmlinuz-*", "vmlinuz"), "kernel")
    initramfs_path = _find_boot_file(root, initramfs, ("initramfs-*.img", "initramfs.img"), "initramfs")
    out = Path(output).resolve()
    volume = _validate_volume_id(volume_id)
    commands = (
        (
            "mksquashfs", str(root), str(squashfs), "-noappend", "-comp", "zstd",
            "-wildcards", "-e", "proc/*", "sys/*", "dev/*", "run/*", "tmp/*", "mnt/*", "media/*",
        ),
        ("grub-mkrescue", "-o", str(out), str(iso_root), "--", "-volid", volume),
    )
    return LiveISOPlan(root, out, volume, architecture, kernel_path, initramfs_path, squashfs, iso_root, commands)


def build_live_iso(
    source_root: Path | str,
    output: Path | str,
    *,
    volume_id: str = "LPM_LIVE",
    architecture: str = "x86_64-v2",
    kernel: str | None = None,
    initramfs: str | None = None,
    dry_run: bool = False,
    staging_root: Path | str | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, object]:
    temporary = None
    if staging_root is None:
        temporary = tempfile.TemporaryDirectory(prefix="lpm-live-iso-")
        stage = Path(temporary.name)
    else:
        stage = Path(staging_root)
    try:
        plan = plan_live_iso(
            source_root, output, volume_id=volume_id, architecture=architecture,
            kernel=kernel, initramfs=initramfs, staging_root=stage,
        )
        result: dict[str, object] = {
            "source_root": str(plan.source_root), "output": str(plan.output),
            "volume_id": plan.volume_id, "architecture": plan.architecture,
            "kernel": str(plan.kernel), "initramfs": str(plan.initramfs),
            "commands": [list(command) for command in plan.commands], "dry_run": dry_run,
        }
        if dry_run:
            return result
        missing = [tool for tool in ("mksquashfs", "grub-mkrescue", "xorriso") if shutil.which(tool) is None]
        if missing:
            raise RuntimeError("missing ISO build tools: " + ", ".join(missing))
        (plan.iso_root / "boot/grub").mkdir(parents=True, exist_ok=True)
        plan.squashfs.parent.mkdir(parents=True, exist_ok=True)
        plan.output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(plan.kernel, plan.iso_root / "boot/vmlinuz")
        shutil.copy2(plan.initramfs, plan.iso_root / "boot/initramfs.img")
        grub_cfg = (
            "set default=0\nset timeout=5\n"
            f"menuentry 'LPM Live ({plan.architecture})' {{\n"
            f"  linux /boot/vmlinuz root=live:CDLABEL={plan.volume_id} rd.live.image ro quiet\n"
            "  initrd /boot/initramfs.img\n}\n"
        )
        (plan.iso_root / "boot/grub/grub.cfg").write_text(grub_cfg, encoding="utf-8")
        (plan.iso_root / "live/build.json").write_text(
            json.dumps({"architecture": plan.architecture, "volume_id": plan.volume_id}, indent=2) + "\n",
            encoding="utf-8",
        )
        for command in plan.commands:
            runner(list(command), check=True)
        if not plan.output.is_file() or plan.output.stat().st_size == 0:
            raise RuntimeError(f"ISO builder did not create output: {plan.output}")
        result["size"] = plan.output.stat().st_size
        return result
    finally:
        if temporary is not None:
            temporary.cleanup()


__all__ = ["LiveISOPlan", "build_live_iso", "plan_live_iso"]
