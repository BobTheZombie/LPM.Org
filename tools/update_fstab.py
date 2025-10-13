#!/usr/bin/env python3
"""Generate an ``fstab`` from the currently mounted block devices.

This helper can be invoked during initial system configuration or later
updates to refresh ``/etc/fstab`` with the devices that are mounted on the
running system.  The script inspects ``/proc/mounts`` and ``/proc/swaps`` in
combination with ``blkid`` metadata to derive stable identifiers (UUIDs or
PARTUUIDs) and sensible defaults for mount options.

The resulting file mirrors the live layout of the system which makes it
useful for reproducing installations or updating images after partitioning
changes.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Sequence


# Filesystem types that should never end up in fstab.
_PSEUDO_FS_TYPES = {
    "autofs",
    "binfmt_misc",
    "bpf",
    "cgroup",
    "cgroup2",
    "configfs",
    "debugfs",
    "devpts",
    "devtmpfs",
    "efivarfs",
    "fusectl",
    "hugetlbfs",
    "mqueue",
    "nfsd",
    "proc",
    "pstore",
    "ramfs",
    "rpc_pipefs",
    "securityfs",
    "sysfs",
    "tmpfs",
    "tracefs",
}

# Filesystem types that do not need ``fsck``.
_NO_FSCK_TYPES = {"btrfs", "xfs", "f2fs", "zfs", "swap"}

# Mount point prefixes that normally only contain runtime mounts.
_EXCLUDED_PREFIXES = ("/proc", "/run", "/sys", "/dev")


def _unescape(value: str) -> str:
    """Convert escape sequences from ``/proc/mounts`` to their real form."""

    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _escape(value: str) -> str:
    """Escape values for ``fstab`` entries."""

    return (
        value.replace("\\", "\\134")
        .replace(" ", "\\040")
        .replace("\t", "\\011")
        .replace("\n", "\\012")
    )


@dataclass
class MountInfo:
    device: str
    target: str
    fs_type: str
    options: str


@dataclass
class SwapInfo:
    device: str
    options: str = "defaults"


@dataclass
class FstabEntry:
    spec: str
    target: str
    fs_type: str
    options: str
    dump: int
    passno: int

    def as_line(self) -> str:
        return "\t".join(
            (
                _escape(self.spec),
                _escape(self.target),
                self.fs_type,
                self.options or "defaults",
                str(self.dump),
                str(self.passno),
            )
        )


def _parse_blkid_export(text: str) -> Dict[str, Dict[str, str]]:
    """Parse the ``blkid -o export`` output into a mapping."""

    data: Dict[str, Dict[str, str]] = {}
    current: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            dev = current.get("DEVNAME")
            if dev:
                resolved = os.path.realpath(dev)
                data[dev] = {k: v for k, v in current.items() if k != "DEVNAME"}
                if resolved != dev:
                    data[resolved] = data[dev]
            current = {}
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        current[key] = value
    if current.get("DEVNAME"):
        dev = current["DEVNAME"]
        resolved = os.path.realpath(dev)
        data[dev] = {k: v for k, v in current.items() if k != "DEVNAME"}
        if resolved != dev:
            data[resolved] = data[dev]
    return data


def _load_blkid_map() -> Dict[str, Dict[str, str]]:
    try:
        proc = subprocess.run(
            ["blkid", "-o", "export"],
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return {}
    if proc.returncode != 0:
        return {}
    return _parse_blkid_export(proc.stdout)


def _parse_proc_mounts(text: str) -> List[MountInfo]:
    entries: List[MountInfo] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        device, target, fs_type, options = parts[:4]
        device = _unescape(device)
        target = _unescape(target)
        fs_type = fs_type.strip()
        options = options.strip()
        entries.append(MountInfo(device=device, target=target, fs_type=fs_type, options=options))
    return entries


def _load_proc_mounts() -> List[MountInfo]:
    try:
        text = Path("/proc/mounts").read_text(encoding="utf-8")
    except OSError:
        return []
    return _parse_proc_mounts(text)


def _parse_proc_swaps(text: str) -> List[SwapInfo]:
    entries: List[SwapInfo] = []
    lines = text.splitlines()
    for line in lines[1:]:  # Skip header line
        if not line.strip():
            continue
        parts = line.split()
        if not parts:
            continue
        device = _unescape(parts[0])
        entries.append(SwapInfo(device=device))
    return entries


def _load_proc_swaps() -> List[SwapInfo]:
    try:
        text = Path("/proc/swaps").read_text(encoding="utf-8")
    except OSError:
        return []
    return _parse_proc_swaps(text)


def _should_include_mount(entry: MountInfo) -> bool:
    if entry.fs_type in _PSEUDO_FS_TYPES:
        return False
    if entry.target != "/" and entry.target.startswith(_EXCLUDED_PREFIXES):
        return False
    if entry.device in {"overlay", "rootfs", "systemd-1"}:
        return False
    if "bind" in entry.options.split(","):
        return False
    return True


def _lookup_identifier(device: str, blk_map: Dict[str, Dict[str, str]]) -> str:
    # Honour entries that are already specified via UUID/PARTUUID/LABEL.
    if device.startswith(("UUID=", "PARTUUID=", "LABEL=")):
        return device

    info = blk_map.get(device) or blk_map.get(os.path.realpath(device))
    if info:
        if info.get("UUID"):
            return f"UUID={info['UUID']}"
        if info.get("PARTUUID"):
            return f"PARTUUID={info['PARTUUID']}"
        if info.get("LABEL"):
            return f"LABEL={info['LABEL']}"
    return device


def _normalize_options(options: str) -> str:
    parts = [opt for opt in options.split(",") if opt]
    normalized: List[str] = []
    for opt in parts:
        if opt == "rw":
            continue
        normalized.append(opt)
    if not normalized:
        return "defaults"
    return ",".join(dict.fromkeys(normalized))


def _fsck_pass(target: str, fs_type: str) -> int:
    if fs_type == "swap":
        return 0
    if fs_type in _NO_FSCK_TYPES:
        return 0
    if target == "/":
        return 1
    return 2


def _build_fstab_entries(
    mounts: Sequence[MountInfo],
    swaps: Sequence[SwapInfo],
    blk_map: Dict[str, Dict[str, str]],
) -> List[FstabEntry]:
    entries: List[FstabEntry] = []
    seen_targets = set()

    for mount in mounts:
        if not _should_include_mount(mount):
            continue
        if mount.target in seen_targets:
            continue
        seen_targets.add(mount.target)
        spec = _lookup_identifier(mount.device, blk_map)
        options = _normalize_options(mount.options)
        entries.append(
            FstabEntry(
                spec=spec,
                target=mount.target,
                fs_type=mount.fs_type,
                options=options,
                dump=0,
                passno=_fsck_pass(mount.target, mount.fs_type),
            )
        )

    for swap in swaps:
        spec = _lookup_identifier(swap.device, blk_map)
        entries.append(
            FstabEntry(
                spec=spec,
                target="none",
                fs_type="swap",
                options=swap.options,
                dump=0,
                passno=0,
            )
        )

    def sort_key(entry: FstabEntry) -> tuple[int, str]:
        if entry.fs_type == "swap":
            return (2, entry.target)
        if entry.target == "/":
            return (0, entry.target)
        return (1, entry.target)

    entries.sort(key=sort_key)
    return entries


def _format_entries(entries: Iterable[FstabEntry]) -> str:
    timestamp = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# /etc/fstab: static file system information.",
        f"# Generated by lpm-update-fstab on {timestamp}.",
        "#",
        "# <file system>\t<mount point>\t<type>\t<options>\t<dump>\t<pass>",
    ]
    for entry in entries:
        lines.append(entry.as_line())
    lines.append("")
    return "\n".join(lines)


def _write_output(path: Path, content: str, dry_run: bool) -> None:
    if dry_run:
        sys.stdout.write(content)
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        timestamp = _dt.datetime.now().strftime("%Y%m%d%H%M%S")
        if path.suffix:
            backup = path.with_suffix(path.suffix + f".bak-{timestamp}")
        else:
            backup = path.with_name(path.name + f".bak-{timestamp}")
        shutil.copy2(path, backup)
        print(f"Backed up {path} to {backup}")

    path.write_text(content, encoding="utf-8")
    print(f"Wrote updated fstab to {path}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="/etc/fstab",
        help="Path to write the generated fstab (default: /etc/fstab)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the generated fstab to stdout without writing to disk",
    )
    args = parser.parse_args(argv)

    blk_map = _load_blkid_map()
    mounts = _load_proc_mounts()
    swaps = _load_proc_swaps()

    entries = _build_fstab_entries(mounts, swaps, blk_map)
    if not entries:
        print("No suitable mounts detected; refusing to overwrite fstab.", file=sys.stderr)
        return 1

    content = _format_entries(entries)
    _write_output(Path(args.output), content, args.dry_run)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())

