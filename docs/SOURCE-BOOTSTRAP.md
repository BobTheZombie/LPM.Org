# Source bootstrap and full-system installation

LPM can build a target filesystem from a local tree of `.lpmbuild` recipes.
It reads `REQUIRES`, `BUILD_REQUIRES`, and `PROVIDES`, rejects ambiguous local
providers and dependency cycles, builds packages in dependency-first order,
then installs the generated `.zst` packages into the target root.

Preview a filesystem-only installation:

```sh
sudo lpm bootstrap \
  --target /mnt/lpm-system \
  --lpmbuild-root ./system-recipes \
  --boot-device disk=/dev/nvme0n1,root=/dev/nvme0n1p2,efi=/dev/nvme0n1p1 \
  --efi-dir /boot/efi \
  --dry-run --verbose
```

Remove `--dry-run` after reviewing the plan. Build artifacts and a durable
source-bootstrap manifest are stored below the target by default. Use
`--source-output PATH` to retain them elsewhere. `--include-packages` selects
a subset and its local dependency closure; `--exclude-packages` removes named
recipes.

## Partition plans

Partitioning is driven by an explicit JSON file. For example:

```json
{
  "device": "/dev/nvme0n1",
  "table": "gpt",
  "partitions": [
    {"number": 1, "size": "512M", "fs_type": "vfat", "mountpoint": "/boot/efi", "label": "EFI"},
    {"number": 2, "size": "-", "fs_type": "ext4", "mountpoint": "/", "label": "lpm-root"}
  ]
}
```

Previewing never modifies disks:

```sh
sudo lpm bootstrap --target /mnt/lpm-system \
  --lpmbuild-root ./system-recipes \
  --partition-plan ./layout.json --dry-run --verbose
```

Actual disk changes require the additional `--partition-confirm` flag. LPM
validates the device name, requires exactly one root filesystem, rejects
duplicate/non-absolute mountpoints, creates filesystems, mounts root first,
and generates `/etc/fstab` using UUIDs. This operation destroys the selected
device's existing partition table and filesystems.

The same settings can be placed under `[bootstrap]` in a TOML config. Resume
state is kept at `var/lib/lpm/bootstrap-state.json` inside the target.
