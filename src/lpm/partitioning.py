"""Validated disk layout, formatting, mounting, and fstab support for LPM.

The public API separates planning from execution.  Nothing touches a block
device until :func:`apply_partition_plan` is called with ``confirm=True``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


_DEVICE_RE = re.compile(r"^/dev/(?:[hsv]d[a-z]+|xvd[a-z]+|nvme\d+n\d+|mmcblk\d+)$")
_SAFE_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "f2fs", "vfat", "swap"}


@dataclass(frozen=True)
class PartitionSpec:
    number: int
    size: str
    fs_type: str
    mountpoint: str | None = None
    label: str | None = None
    type_code: str | None = None
    options: str = "defaults"


@dataclass(frozen=True)
class PartitionPlan:
    device: str
    table: str = "gpt"
    partitions: tuple[PartitionSpec, ...] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PartitionPlan":
        entries = tuple(PartitionSpec(**item) for item in value.get("partitions", ()))
        return cls(device=str(value.get("device", "")), table=str(value.get("table", "gpt")), partitions=entries)


def load_partition_plan(path: Path | str) -> PartitionPlan:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("partition plan must be a JSON object")
    plan = PartitionPlan.from_mapping(payload)
    validate_partition_plan(plan)
    return plan


def partition_path(device: str, number: int) -> str:
    separator = "p" if device[-1:].isdigit() else ""
    return f"{device}{separator}{number}"


def validate_partition_plan(plan: PartitionPlan, *, require_device: bool = False) -> None:
    if not _DEVICE_RE.fullmatch(plan.device):
        raise ValueError(f"unsafe or unsupported block device: {plan.device!r}")
    if plan.table not in {"gpt", "dos"}:
        raise ValueError("partition table must be 'gpt' or 'dos'")
    if require_device and (not Path(plan.device).exists() or not _is_block_device(Path(plan.device))):
        raise ValueError(f"not a block device: {plan.device}")
    if not plan.partitions:
        raise ValueError("partition plan is empty")
    numbers: set[int] = set()
    mounts: set[str] = set()
    root_count = 0
    for part in plan.partitions:
        if part.number < 1 or part.number in numbers:
            raise ValueError(f"invalid or duplicate partition number: {part.number}")
        numbers.add(part.number)
        if not re.fullmatch(r"(?:\d+(?:\.\d+)?[KMGTP]?|100%|-)", part.size, re.IGNORECASE):
            raise ValueError(f"invalid partition size: {part.size!r}")
        if part.fs_type not in _SAFE_FS:
            raise ValueError(f"unsupported filesystem: {part.fs_type}")
        if part.fs_type == "swap" and part.mountpoint not in {None, "none"}:
            raise ValueError("swap partitions cannot have a mountpoint")
        if part.mountpoint:
            if not part.mountpoint.startswith("/") or ".." in Path(part.mountpoint).parts:
                raise ValueError(f"unsafe mountpoint: {part.mountpoint!r}")
            if part.mountpoint in mounts:
                raise ValueError(f"duplicate mountpoint: {part.mountpoint}")
            mounts.add(part.mountpoint)
            root_count += part.mountpoint == "/"
    if numbers != set(range(1, len(numbers) + 1)):
        raise ValueError("partition numbers must be contiguous and start at 1")
    if root_count != 1:
        raise ValueError("partition plan must contain exactly one root mountpoint")


def _is_block_device(path: Path) -> bool:
    try:
        return path.is_block_device()
    except OSError:
        return False


def render_sfdisk(plan: PartitionPlan) -> str:
    validate_partition_plan(plan)
    lines = [f"label: {plan.table}", "unit: sectors", ""]
    for part in sorted(plan.partitions, key=lambda item: item.number):
        fields = [f"size={part.size}"] if part.size != "-" else []
        if part.type_code:
            fields.append(f"type={part.type_code}")
        if part.label:
            fields.append(f'name="{part.label}"')
        lines.append(", ".join(fields))
    return "\n".join(lines) + "\n"


def _mkfs_command(device: str, part: PartitionSpec) -> list[str]:
    path = partition_path(device, part.number)
    if part.fs_type == "swap":
        return ["mkswap", *( ["-L", part.label] if part.label else []), path]
    tool = "mkfs.fat" if part.fs_type == "vfat" else f"mkfs.{part.fs_type}"
    args = [tool]
    if part.fs_type in {"ext2", "ext3", "ext4"}:
        args.append("-F")
    elif part.fs_type in {"xfs", "btrfs", "f2fs"}:
        args.append("-f")
    if part.label:
        args.extend(["-n" if part.fs_type == "vfat" else "-L", part.label])
    return [*args, path]


def apply_partition_plan(
    plan: PartitionPlan,
    *,
    confirm: bool = False,
    dry_run: bool = False,
    runner: Callable[..., Any] = subprocess.run,
) -> list[list[str]]:
    if not dry_run and not confirm:
        raise PermissionError("refusing to destroy a partition table without explicit confirmation")
    validate_partition_plan(plan, require_device=not dry_run)
    commands = [["sfdisk", "--wipe", "always", plan.device]]
    commands.extend(_mkfs_command(plan.device, part) for part in sorted(plan.partitions, key=lambda item: item.number))
    if dry_run:
        return commands
    required = {command[0] for command in commands}
    missing = sorted(tool for tool in required if shutil.which(tool) is None)
    if missing:
        raise RuntimeError("missing partitioning tools: " + ", ".join(missing))
    runner(commands[0], input=render_sfdisk(plan), text=True, check=True)
    runner(["partprobe", plan.device], check=False)
    for command in commands[1:]:
        runner(command, check=True)
    return commands


def mount_partition_plan(
    plan: PartitionPlan,
    target: Path | str,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> list[Path]:
    validate_partition_plan(plan)
    root = Path(target).resolve()
    mounted: list[Path] = []
    entries = [part for part in plan.partitions if part.mountpoint and part.fs_type != "swap"]
    entries.sort(key=lambda part: (part.mountpoint != "/", part.mountpoint.count("/"), part.number))
    for part in entries:
        destination = root if part.mountpoint == "/" else root / part.mountpoint.lstrip("/")
        destination.mkdir(parents=True, exist_ok=True)
        runner(["mount", "-o", part.options, partition_path(plan.device, part.number), str(destination)], check=True)
        mounted.append(destination)
    return mounted


def fstab_entries(plan: PartitionPlan, identifiers: Mapping[str, str] | None = None) -> list[str]:
    validate_partition_plan(plan)
    ids = identifiers or {}
    lines = ["# /etc/fstab generated by LPM"]
    for part in sorted(plan.partitions, key=lambda item: (item.mountpoint != "/", item.number)):
        device = partition_path(plan.device, part.number)
        source = ids.get(device, device)
        if part.fs_type == "swap":
            lines.append(f"{source}\tnone\tswap\tdefaults\t0\t0")
            continue
        if not part.mountpoint:
            continue
        passno = 1 if part.mountpoint == "/" and part.fs_type.startswith("ext") else (2 if part.fs_type.startswith("ext") else 0)
        lines.append(f"{source}\t{part.mountpoint}\t{part.fs_type}\t{part.options}\t0\t{passno}")
    return lines


def write_fstab(plan: PartitionPlan, target: Path | str) -> Path:
    identifiers: dict[str, str] = {}
    for part in plan.partitions:
        device = partition_path(plan.device, part.number)
        proc = subprocess.run(["blkid", "-s", "UUID", "-o", "value", device], check=False, capture_output=True, text=True)
        if proc.returncode == 0 and proc.stdout.strip():
            identifiers[device] = f"UUID={proc.stdout.strip()}"
    path = Path(target) / "etc/fstab"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(fstab_entries(plan, identifiers)) + "\n", encoding="utf-8")
    return path


__all__ = ["PartitionPlan", "PartitionSpec", "apply_partition_plan", "fstab_entries", "load_partition_plan", "mount_partition_plan", "partition_path", "render_sfdisk", "validate_partition_plan", "write_fstab"]
