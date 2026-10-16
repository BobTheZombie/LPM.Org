# systemd-boot with LPM

The systemd package supplies bootctl and systemd-boot EFI binaries. This package
adds LPM's configuration; LPM supplies update-systemd-boot.hook and its helper.
Rebuild/install LPM from the revision containing these files before setup.

This integration supports separate kernels and initramfs images (BLS Type #1),
not automatic UKI generation. The firmware must support UEFI. For Secure Boot,
provide appropriately signed EFI binaries and a validated signing workflow;
this package does not enroll keys or sign kernels.

## Initial setup inside the target chroot

1. Mount the existing EFI System Partition at /boot/efi (or change `esp` in
   /etc/lpm/systemd-boot.conf). Add its UUID and mount point to /etc/fstab.
   Do not format an existing partition. /boot/efi is only a default, not an
   instruction to use a particular disk or partition.
2. Install a kernel as /usr/lib/modules/VERSION/vmlinuz.
3. Generate /boot/initrd-VERSION.img using your configured initramfs generator.
   LPM's existing kernel-install helper uses mkinitcpio. Change `initrd` in the
   integration configuration if your generator uses a different filename.
4. Create /etc/kernel/cmdline with your real root device and any required LUKS,
   LVM, root filesystem or resume options. The example is deliberately not
   installed as the real command line. Never copy the host's /proc/cmdline
   blindly into a new system.
5. Run:

       /usr/libexec/lpm/hooks/update-systemd-boot --install

   This explicitly installs bootloader files without changing firmware NVRAM
   (`bootctl --no-variables install`). To create/select a firmware entry, run
   bootctl --esp-path=/boot/efi install explicitly from an appropriate UEFI
   environment. Neither command signs binaries for Secure Boot.
6. Verify:

       bootctl --esp-path=/boot/efi status
       bootctl --esp-path=/boot/efi list
       cat /boot/efi/loader/entries/lpm-*.conf

The generated loader.conf defaults to `lpm-*`, timeout 5 and editor disabled.
An existing loader.conf is preserved. The system's kernel-install.conf and
other bootloader entries are not changed. Kernels and initramfs files are
copied onto the ESP so paths in the entries are accessible to systemd-boot.

## Updates

The transaction hook responds to bootloader EFI binaries, versioned kernels,
initramfs images and configuration changes. It updates only when systemd-boot
is already installed on the configured, mounted ESP. Alternate-root LPM
transactions skip the helper; run setup inside the prepared target chroot.
The existing LPM kernel helper calls this updater after generating initramfs.

    /usr/libexec/lpm/hooks/update-systemd-boot

Kernel files and entries are copied atomically, with the entry published after
its payload. Updates are serialized using /var/lib/lpm/systemd-boot.lock.
Other boot entries and loader settings are preserved. Entries from removed
kernels are retained as recovery copies; remove their lpm-VERSION.conf and
corresponding ESP/lpm/VERSION directory manually after checking a newer boot.
There is no automatic kernel/boot entry pruning.

Packaging ownership: systemd owns bootctl/EFI binaries; lpm owns the hook and
helper; systemd-boot owns /etc/lpm/systemd-boot.conf and these setup examples.
No ESP or firmware modifications occur during package staging.
