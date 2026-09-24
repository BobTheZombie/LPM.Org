# jhalfs LPM bootstrap

This profile turns upstream jhalfs into a reproducible systemd-based bootstrap
root for LPM. jhalfs builds the canonical LFS base and kernel; ordered custom
stages then install LPM, build the networking/runtime packages from the LPM
recipe repository, configure dbus-broker and NetworkManager, generate the
initramfs, and validate the result.

The resulting root is intentionally a build/bootstrap environment. It is not a
desktop image and it does not install a boot loader or partition disks.

## Host requirements

- A Linux host satisfying the current LFS host-system requirements
- Git, GNU Make, Python 3, sudo, wget, xsltproc and the DocBook XML/XSL data
- A dedicated target filesystem mounted at `LFS_ROOT`
- A standalone LPM executable built from this repository
- Enough free space for an LFS toolchain and package builds (40 GiB minimum;
  60 GiB recommended)

Never point `LFS_ROOT` at `/`, `/home`, or a directory containing valuable
data. jhalfs performs privileged operations below this path.

## Build LPM

Build the standalone command-line executable before starting jhalfs:

```bash
make build/nuitka/lpm.bin
```

The graphical `lpm-ui` is not included in the bootstrap root.

## Configure and build

Mount the target filesystem, then run:

```bash
export LFS_ROOT=/mnt/lpm-lfs

sudo install -d -m755 "$LFS_ROOT"
sudo chown root:root "$LFS_ROOT"

bootstrap/jhalfs-lpm/build.sh prepare
bootstrap/jhalfs-lpm/build.sh configure
bootstrap/jhalfs-lpm/build.sh generate
bootstrap/jhalfs-lpm/build.sh build
bootstrap/jhalfs-lpm/build.sh validate
```

The configuration interface must retain these settings:

- **Linux From Scratch — systemd**
- **chroot** build method
- target build directory equal to `LFS_ROOT`
- source retrieval enabled
- custom tools enabled
- automatic Makefile execution disabled

After configuration, the remaining phases can be repeated independently. A
failed jhalfs target can be resumed using its generated Makefile.

## Package stage

[`config/packages.list`](config/packages.list) defines the explicit final
packages. LPM resolves and builds their transitive dependencies from a pinned
local checkout of the package recipe repository. The defaults install:

- dbus-broker
- wireless-regdb and iw
- wpa_supplicant
- NetworkManager
- linux-firmware
- mkinitcpio

Override the package repository or branch when necessary:

```bash
PACKAGE_REPO=https://gitlab.com/lpm-org/packages.git \
PACKAGE_REF=main \
bootstrap/jhalfs-lpm/build.sh generate
```

## Service model

dbus-broker is the only configured system-bus implementation. `dbus.service`
and `dbus-org.freedesktop.DBus.service` resolve to `dbus-broker.service`.
NetworkManager uses systemd-resolved, and systemd-timesyncd is enabled.

The generated initramfs uses systemd-based mkinitcpio hooks and zstd
compression. Generation is attempted when an mkinitcpio preset exists; root
validation deliberately fails when no initramfs was produced.

## Handoff

Once validation passes, enter the root with the normal virtual filesystems
mounted:

```bash
mount --bind /dev "$LFS_ROOT/dev"
mount -t devpts devpts "$LFS_ROOT/dev/pts" -o gid=5,mode=0620
mount -t proc proc "$LFS_ROOT/proc"
mount -t sysfs sysfs "$LFS_ROOT/sys"
mount -t tmpfs tmpfs "$LFS_ROOT/run"

chroot "$LFS_ROOT" /usr/bin/env -i \
    HOME=/root \
    TERM="$TERM" \
    PATH=/usr/bin:/usr/sbin \
    /bin/bash --login
```

Inside the root, `lpm verify` should report a consistent database for all
packages installed by the LPM post-system stage. Files installed earlier by
jhalfs remain bootstrap-owned; use this root to have LPM construct a separate,
fully package-owned final root when strict ownership is required.
