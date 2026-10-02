# Source bootstrap and full-system installation

LPM can build a target filesystem from a local tree of `.lpmbuild` recipes.
It reads `REQUIRES`, `BUILD_REQUIRES`, and `PROVIDES`, rejects ambiguous local
providers and dependency cycles, builds packages in dependency-first order,
then installs the generated `.zst` packages into the target root.

For a build chroot that is not directly bootable, pass `--chroot-only`. This
removes the root-device requirement and skips initramfs and bootloader stages:

```sh
sudo lpm bootstrap \
  --target /mnt/lpm-chroot \
  --chroot-only \
  --lpmbuild-root /home/build/packages \
  --prepare-lfs-book \
  --book-version 13.1-systemd \
  --verbose
```

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
`var/cache/lpm/books` in the target. The extracted `instructions.json`,
`phase-plan.json`, raw command scripts, and section scripts are written below
`var/lib/lpm/jhalfs/VERSION`. A section is the executable unit, so all command
blocks in one book section share the same shell and retain `cd`/export state.

Use `--book-cache PATH` to keep the cache outside the target,
`--book-refresh` to redownload the pinned inputs, or `--book-offline` to reject
all downloads and require a complete verified cache. Supplying
`--book-sha256` is strongly recommended for the first download; subsequent
runs also verify the digest recorded in the cache metadata. Extraction alone
does not execute book commands. Phased execution is an explicit opt-in:

```sh
sudo lpm bootstrap \
  --target /mnt/lpm-chroot \
  --chroot-only \
  --prepare-lfs-book \
  --execute-lfs-phases \
  --lfs-phase cross-toolchain \
  --lfs-phase temporary-tools \
  --lfs-only \
  --resume --verbose
```

Before execution, LPM downloads the upstream `wget-list` into
`TARGET/sources` and validates every file against the book's `md5sums`. If an
upstream URL has disappeared, LPM retries official LFS file mirrors using the
pinned release path; no mirror result is accepted unless its checksum matches.
Available phases are `cross-toolchain`, `temporary-tools`, `chroot-tools`,
`final-system`, `system-configuration`, and `boot`. Chapters 5 and 6 execute
as the unprivileged `lfs` user (override with `--lfs-user`); later phases run
inside the target chroot. Each successful section is checkpointed with its
SHA-256 in `execution-state.json`. With `--resume`, a modified script is
rejected rather than silently skipped.

Sections which create the chroot boundary or mount virtual filesystems are
owned by the LPM orchestrator and skipped. `--lfs-allow-manual` overrides that
guard deliberately. `--lfs-only` stops after the book phases and prevents the
ordinary lpmbuild dependency resolver from trying to resolve the entire
desktop package repository. Phase prerequisites are enforced; `--force` is
required to start a later phase without matching earlier checkpoints.

The host must have the selected unprivileged account. For a conventional LFS
builder account:

```sh
sudo groupadd -f lfs
id lfs >/dev/null 2>&1 || \
  sudo useradd -s /bin/bash -g lfs -m -k /dev/null lfs
```

Equivalent TOML settings are:

```toml
[bootstrap]
book_enabled = true
book_version = "13.1-systemd"
book_sha256 = "...64 hexadecimal characters..."
book_offline = false
book_refresh = false
lfs_execute = true
lfs_phases = ["cross-toolchain", "temporary-tools"]
lfs_user = "lfs"
lfs_only = true
```
