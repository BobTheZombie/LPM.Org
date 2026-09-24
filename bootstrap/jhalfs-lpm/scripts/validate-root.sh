#!/usr/bin/env bash
set -euo pipefail

root="${LFS_ROOT:?LFS_ROOT must name the completed target root}"
failures=0

require_file() {
    local path="$1"
    if [[ ! -e "$root$path" && ! -L "$root$path" ]]; then
        echo "missing: $path" >&2
        failures=$((failures + 1))
    fi
}

require_executable() {
    local path="$1"
    if [[ ! -x "$root$path" ]]; then
        echo "missing executable: $path" >&2
        failures=$((failures + 1))
    fi
}

require_executable /usr/lib/systemd/systemd
require_executable /usr/bin/systemctl
require_executable /usr/bin/dbus-broker-launch
require_executable /usr/bin/nmcli
require_executable /usr/sbin/wpa_supplicant
require_executable /usr/bin/python3
require_executable /usr/bin/lpm
require_executable /usr/bin/zstd

require_file /usr/lib/systemd/system/dbus-broker.service
require_file /usr/lib/systemd/system/NetworkManager.service
require_file /etc/NetworkManager/conf.d/10-lpm.conf
require_file /etc/mkinitcpio.conf

if ! compgen -G "$root/boot/vmlinuz-*" >/dev/null && \
   [[ ! -e "$root/boot/vmlinuz" ]]; then
    echo "missing: kernel image under /boot" >&2
    failures=$((failures + 1))
fi

if ! compgen -G "$root/boot/initramfs-*.img" >/dev/null; then
    echo "missing: initramfs image under /boot" >&2
    failures=$((failures + 1))
fi

if (( failures > 0 )); then
    echo "LPM bootstrap root validation failed: $failures problem(s)" >&2
    exit 1
fi

echo "LPM bootstrap root validated successfully: $root"
