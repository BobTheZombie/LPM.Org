from __future__ import annotations

from tools import update_fstab


BLKID_SAMPLE = """\
DEVNAME=/dev/sda1
UUID=1111-AAAA
TYPE=ext4

DEVNAME=/dev/sda2
UUID=2222-BBBB
TYPE=vfat

DEVNAME=/dev/sda3
UUID=3333-CCCC
TYPE=swap
"""


MOUNTS_SAMPLE = """\
/dev/sda1 / ext4 rw,relatime 0 0
/dev/sda2 /boot/efi vfat rw,fmask=0022,dmask=0022 0 0
tmpfs /run tmpfs rw,nosuid,nodev,mode=755 0 0
"""


SWAPS_SAMPLE = """\
Filename\tType\tSize\tUsed\tPriority
/dev/sda3\tpartition\t2097148\t0\t-2
"""


def test_parse_blkid_export() -> None:
    data = update_fstab._parse_blkid_export(BLKID_SAMPLE)
    assert data["/dev/sda1"]["UUID"] == "1111-AAAA"
    assert data["/dev/sda2"]["TYPE"] == "vfat"


def test_parse_proc_mounts() -> None:
    mounts = update_fstab._parse_proc_mounts(MOUNTS_SAMPLE)
    assert mounts[0].device == "/dev/sda1"
    assert mounts[0].target == "/"
    assert mounts[1].target == "/boot/efi"


def test_parse_proc_swaps() -> None:
    swaps = update_fstab._parse_proc_swaps(SWAPS_SAMPLE)
    assert swaps[0].device == "/dev/sda3"


def test_build_fstab_entries() -> None:
    blk_map = update_fstab._parse_blkid_export(BLKID_SAMPLE)
    mounts = update_fstab._parse_proc_mounts(MOUNTS_SAMPLE)
    swaps = update_fstab._parse_proc_swaps(SWAPS_SAMPLE)
    entries = update_fstab._build_fstab_entries(mounts, swaps, blk_map)

    assert entries[0].target == "/"
    root_entry = entries[0]
    assert root_entry.spec == "UUID=1111-AAAA"
    assert root_entry.fs_type == "ext4"
    assert root_entry.passno == 1

    boot_entry = next(e for e in entries if e.target == "/boot/efi")
    assert boot_entry.spec == "UUID=2222-BBBB"
    assert boot_entry.fs_type == "vfat"
    assert boot_entry.passno == 2

    swap_entry = next(e for e in entries if e.fs_type == "swap")
    assert swap_entry.spec == "UUID=3333-CCCC"
    assert swap_entry.target == "none"


def test_format_entries() -> None:
    blk_map = update_fstab._parse_blkid_export(BLKID_SAMPLE)
    mounts = update_fstab._parse_proc_mounts(MOUNTS_SAMPLE)
    swaps = update_fstab._parse_proc_swaps(SWAPS_SAMPLE)
    entries = update_fstab._build_fstab_entries(mounts, swaps, blk_map)
    text = update_fstab._format_entries(entries)
    assert "UUID=1111-AAAA" in text
    assert "UUID=3333-CCCC" in text

