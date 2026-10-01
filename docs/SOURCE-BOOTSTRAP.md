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

## Cached LFS book instructions

The bootstrap can pin a rendered Linux From Scratch systemd book, cache it,
and extract each `<pre class="userinput">` command block into a numbered Bash
script. The default book is `13.1-systemd`; changing versions is explicit.

```sh
sudo lpm bootstrap \
  --target /mnt/lpm-system \
  --lpmbuild-root ./system-recipes \
  --prepare-lfs-book \
  --book-version 13.1-systemd \
  --book-sha256 SHA256_OF_THE_NOCHUNKS_HTML \
  --dry-run --verbose
```

The versioned cache contains the no-chunks HTML book, upstream `wget-list`,
`md5sums`, and `cache.json`. By default it is stored below
`var/cache/lpm/books` in the target. The extracted `instructions.json` and
executable scripts are written below `var/lib/lpm/jhalfs/VERSION`.

Use `--book-cache PATH` to keep the cache outside the target,
`--book-refresh` to redownload the pinned inputs, or `--book-offline` to reject
all downloads and require a complete verified cache. Supplying
`--book-sha256` is strongly recommended for the first download; subsequent
runs also verify the digest recorded in the cache metadata. Extraction does
not execute book commands. It produces auditable inputs for later bootstrap
stages.

Equivalent TOML settings are:

```toml
[bootstrap]
book_enabled = true
book_version = "13.1-systemd"
book_sha256 = "...64 hexadecimal characters..."
book_offline = false
book_refresh = false
```
