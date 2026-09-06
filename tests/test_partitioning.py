from pathlib import Path

import pytest

from lpm.partitioning import (
    PartitionPlan,
    PartitionSpec,
    apply_partition_plan,
    fstab_entries,
    partition_path,
    render_sfdisk,
    validate_partition_plan,
)


def _plan() -> PartitionPlan:
    return PartitionPlan(
        device="/dev/nvme0n1",
        partitions=(
            PartitionSpec(1, "512M", "vfat", "/boot/efi", "EFI", "U"),
            PartitionSpec(2, "-", "ext4", "/", "root"),
        ),
    )


def test_partition_plan_renders_nvme_paths_and_fstab() -> None:
    plan = _plan()
    assert partition_path(plan.device, 2) == "/dev/nvme0n1p2"
    assert "label: gpt" in render_sfdisk(plan)
    lines = fstab_entries(plan, {"/dev/nvme0n1p2": "UUID=ROOT"})
    assert any(line.startswith("UUID=ROOT\t/\text4") for line in lines)


def test_partition_execution_requires_confirmation() -> None:
    with pytest.raises(PermissionError):
        apply_partition_plan(_plan(), confirm=False, dry_run=False)


def test_partition_dry_run_never_requires_real_device() -> None:
    commands = apply_partition_plan(_plan(), dry_run=True)
    assert commands[0] == ["sfdisk", "--wipe", "always", "/dev/nvme0n1"]
    assert commands[-1][-1] == "/dev/nvme0n1p2"


def test_partition_plan_rejects_missing_root() -> None:
    plan = PartitionPlan("/dev/sda", partitions=(PartitionSpec(1, "1G", "swap"),))
    with pytest.raises(ValueError, match="root mountpoint"):
        validate_partition_plan(plan)
