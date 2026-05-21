from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import subprocess
from typing import Iterable, List


@dataclass(frozen=True)
class MountSpec:
    source: str
    rel_target: str
    fs_type: str
    options: tuple[str, ...] = ()


CHROOT_API_MOUNTS: tuple[MountSpec, ...] = (
    MountSpec("proc", "proc", "proc"),
    MountSpec("sysfs", "sys", "sysfs"),
    MountSpec("/dev", "dev", "none", ("bind",)),
    MountSpec("/dev/pts", "dev/pts", "none", ("bind",)),
    MountSpec("tmpfs", "run", "tmpfs"),
)


@dataclass
class ChrootMountState:
    mounted: list[str] = field(default_factory=list)

    def mark_mounted(self, rel_target: str) -> None:
        if rel_target not in self.mounted:
            self.mounted.append(rel_target)

    def mark_unmounted(self, rel_target: str) -> None:
        self.mounted = [m for m in self.mounted if m != rel_target]


def _mount_table() -> set[str]:
    points: set[str] = set()
    with Path("/proc/self/mounts").open("r", encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) >= 2:
                points.add(parts[1])
    return points


def _is_mounted(target: Path, known_mounts: set[str] | None = None) -> bool:
    mounts = known_mounts if known_mounts is not None else _mount_table()
    return str(target) in mounts


def _mount_cmd(spec: MountSpec, target: Path) -> list[str]:
    cmd = ["mount"]
    if "bind" in spec.options:
        cmd.extend(["--bind", spec.source, str(target)])
        return cmd
    cmd.extend(["-t", spec.fs_type, spec.source, str(target)])
    return cmd


def _run(cmd: Iterable[str]) -> None:
    subprocess.run(list(cmd), check=True)


def mount_chroot_api(target: str | Path, mount_state: ChrootMountState | None = None) -> ChrootMountState:
    base = Path(target)
    state = mount_state or ChrootMountState()
    known_mounts = _mount_table()
    newly_mounted: list[str] = []
    try:
        for spec in CHROOT_API_MOUNTS:
            rel_target = spec.rel_target
            mountpoint = base / rel_target
            if rel_target in state.mounted or _is_mounted(mountpoint, known_mounts):
                state.mark_mounted(rel_target)
                continue
            mountpoint.mkdir(parents=True, exist_ok=True)
            _run(_mount_cmd(spec, mountpoint))
            state.mark_mounted(rel_target)
            known_mounts.add(str(mountpoint))
            newly_mounted.append(rel_target)
        return state
    except Exception:
        umount_chroot_api(base, ChrootMountState(mounted=newly_mounted))
        raise


def umount_chroot_api(target: str | Path, mount_state: ChrootMountState) -> ChrootMountState:
    base = Path(target)
    known_mounts = _mount_table()
    for rel_target in reversed(mount_state.mounted):
        mountpoint = base / rel_target
        if not _is_mounted(mountpoint, known_mounts):
            mount_state.mark_unmounted(rel_target)
            continue
        _run(["umount", str(mountpoint)])
        mount_state.mark_unmounted(rel_target)
        known_mounts.discard(str(mountpoint))
    return mount_state
