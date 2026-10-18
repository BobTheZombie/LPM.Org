# kmake: LPM kernel builds

`kmake` orchestrates upstream Kbuild. It does not install into the running
system, generate an initramfs, change boot entries, or download source. LPM
packages and transaction hooks retain those responsibilities.

Both `kmake ...` and `lpm kmake ...` are available. Before installing the new
LPM build, run `PYTHONPATH=src python3 -m lpm.kmake ...` from this repository.
Global options precede the subcommand. This first version supports x86_64 and
arm64 with GCC; target instructions and hardware coverage come from Kconfig,
not the CPU on which the build happens.

## Existing full configuration

From your extracted kernel source directory:

```sh
kmake --source . --output "$PWD/_build" --arch x86_64 \
    --localversion=-lpm2 configure --config /sources/packages/linux-kernel/config-7.0.6 \
    --require EFI_STUB --require EXT4_FS --require SATA_AHCI --require BLK_DEV_SD
kmake --source . --output "$PWD/_build" build
kmake --source . --output "$PWD/_build" status
kmake --source . --output "$PWD/_build" check
kmake --source . --output "$PWD/_build" stage --destdir /tmp/pkg-linux
```

A required symbol is checked after Kconfig resolves dependencies; `kmake`
does not silently enable drivers. Configure them in the baseline or supply
`--fragment boot.fragment`. Use `configure --defconfig` only if you deliberately
want architecture defaults. Baselines/fragments are executable-tool inputs from
a trusted source, just like the kernel Makefile itself.

The configure step disables automatic git local-version suffixes and clears
extra trusted/revocation certificate paths. It clears CONFIG_LOCALVERSION so
`--localversion` controls the suffix. It does not disable module signing or
Secure Boot enforcement, remove debug information, or decide which root-disk
and graphics drivers you need.

## Resume and state

`build` always invokes Kbuild, which reuses objects and recompiles changed
sources. No command runs clean, mrproper, or deletes the output tree. An
interrupted/failed build is recorded as incomplete and can be resumed with the
same `build` command. No wall-clock or speedup claim replaces Kbuild's dependency
tracking.

`_build/.kmake.json` records configuration/input hashes, release, source path,
source Makefile hash, compiler command/version, and successful phase completion.
An advisory output lock prevents concurrent kmake operations in the same tree.
Configuration, architecture, suffix, or compiler changes require `configure`
again. To retain manual `.config` edits, run `configure` without `--config`.
Re-importing a baseline resets configuration to that input; the previous config
is saved as `.config.kmake-backup`. Do not run a separate bare make concurrently
with kmake, as bare make does not honor kmake's lock.

## Accelerators

- Automatic job selection reuses LPM's affinity/cgroup CPU limit and available
  memory detection, reserving 20% memory with a default 1536 MiB per compile job.
- `--jobs N` or `LPM_BUILD_JOBS=N` overrides the automatic budget.
- `--memory-per-job N` tunes the automatic memory budget; use a higher budget
  for particularly memory-intensive configurations. `--load-average N` adds GNU
  make's load limit. This is a scheduling budget, not a hard RAM guarantee.
- `--cache auto` detects ccache then sccache. `--cache none` adds no cache;
  explicitly supplied compiler wrappers remain authoritative. Explicit cache
  selection fails clearly if unavailable. `--cache-dir` selects persistent
  storage outside the output tree. No unsafe ccache sloppiness is enabled.
- CC, HOSTCC and HOSTCXX cache commands are passed as make command-line
  assignments, which Kbuild honors. Distro CFLAGS, LDFLAGS, KCFLAGS and inherited
  make/jobserver flags are cleared. Compiler caches remain optional packages.

Options configuring caches/toolchains should be consistent across configure,
build, check, and stage. `CC`, `HOSTCC`, `HOSTCXX` and `CROSS_COMPILE` are explicit
user toolchain choices. An accelerator cannot make a missing compiler available.

## LPM recipe phases

Add `lpm` to BUILD_REQUIRES. Include your full config and optional fragments in
SOURCE so LPM stages them under SRCROOT. Keep BUILD_OPTIONS empty for Kbuild.

```bash
prepare() {
    set -euxo pipefail
    cd "$SRCROOT/${NAME}-${VERSION}"
    kmake --source "$PWD" --output "$PWD/_build" --arch x86_64 \
        --localversion="-lpm${RELEASE}" configure \
        --config "$SRCROOT/config-7.0.6" \
        --require EFI_STUB --require SATA_AHCI --require EXT4_FS
}
build() {
    set -euxo pipefail
    cd "$SRCROOT/${NAME}-${VERSION}"
    kmake --source "$PWD" build
}
check() {
    set -euxo pipefail
    cd "$SRCROOT/${NAME}-${VERSION}"
    kmake --source "$PWD" check
}
staging() {
    set -euxo pipefail
    cd "$SRCROOT/${NAME}-${VERSION}"
    kmake --source "$PWD" stage --destdir "$pkgdir" --pkgbase "$NAME"
    install -Dm644 COPYING "$pkgdir/usr/share/licenses/$NAME/COPYING"
}
```

Staging requires a completed, matching build and an empty destination kernel
version directory. It installs modules with stripped debug sections, writes
`vmlinuz`, `System.map`, `config`, and `pkgbase` beneath
`usr/lib/modules/<release>`, removes build/source links, and runs depmod against
DESTDIR. It does not install userspace kernel headers or perform boot deployment.
Use a fresh package staging directory when retrying a partially failed stage.

The output directory persists only if LPM preserves its source/output storage.
Kmake cannot recover objects that LPM or another process has already deleted.

## Validation limits

Mock Kbuild integration exercises interruption/retry, stale config/toolchain
rejection, staging, and resource/cache controls. Tests against real Kconfig
validate configuration and release resolution. Neither test is a complete kernel
build, GCC 16 validation, or a hardware boot test. Run those on the target build
machine before treating a generated kernel as boot validated.
