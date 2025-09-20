# LPM.Org

The Linux Package Manager

## Feature Overview

- **SAT solver based dependency resolution** with support for conflicts,
  provides/obsoletes, package pinning and signature verification.
- **CPU aware build optimisation** – detects the host processor and sets
  appropriate `-march`/`-mtune`, `-pipe`, `-fPIC` and optimisation level flags
  (`OPT_LEVEL` in `/etc/lpm/lpm.conf`).
- **Filesystem snapshots** stored in `/var/lib/lpm/snapshots` with automatic
  pruning (`MAX_SNAPSHOTS` in `lpm.conf`) and rollback support.
- **Bootstrap mode** to build a minimal chroot and populate it with verified
  packages.
- **Incremental SAT solver API** available for other tools and benchmarks in
  `benchmarks/solver_bench.py`.
- **.lpmbuild scripts** for reproducible package builds and a `build` command to
  package staged roots.

## Documentation

The new [Technical How-To](docs/TECHNICAL-HOWTO.md) walks through every
end-user command in depth, including configuration, repository management,
package workflows, snapshotting, verification, and package creation. Refer to
it whenever you need detailed examples or flag reference material.

## Command line interface

`lpm` uses sub‑commands. Each command listed below shows its required
arguments and optional flags.

- `lpm setup` – launch the interactive first-run configuration wizard. The
  wizard also runs automatically the first time `lpm` starts if
  `/etc/lpm/lpm.conf` is missing.
- `lpm clean` – purge cached package blobs from `${XDG_CACHE_HOME:-~/.cache}/lpm`.

### Repository management

- `lpm repolist` – list configured repositories.
- `lpm repoadd NAME URL [--priority N]` – add a repository.
- `lpm repodel NAME` – remove a repository.

### Package discovery

- `lpm search [PATTERN ...]` – search repositories.
- `lpm info NAME...` – show package metadata.

### Package installation and removal

- `lpm install NAME... [--root PATH] [--dry-run] [--no-verify] [--allow-fallback|--no-fallback]`
- `lpm remove NAME... [--root PATH] [--dry-run] [--force]`
- `lpm autoremove [--root PATH] [--dry-run]` – uninstall orphaned dependencies.
- `lpm upgrade [NAME ...] [--root PATH] [--dry-run] [--no-verify] [--allow-fallback|--no-fallback] [--force]`
- `lpm list` – list installed packages.
- `lpm files NAME` – list files that belong to an installed package.
- `lpm verify [--root PATH]` – verify that installed files exist.

When building a package, LPM automatically generates a post-install script if
the `.lpmbuild` script does not provide one. The script inspects the package
contents and runs common maintenance commands based on what it finds:

- A desktop entry such as
  ```
  usr/share/applications/foo.desktop
  ```
  triggers `update-desktop-database "$LPM_ROOT/usr/share/applications"`.
- An icon theme containing
  ```
  usr/share/icons/hicolor/index.theme
  ```
  triggers `gtk-update-icon-cache "$LPM_ROOT/usr/share/icons/hicolor"`.
- Shared libraries like
  ```
  usr/lib/libfoo.so
  ```
  trigger `ldconfig` when installing into the real root (`/`).

### Snapshot management

- `lpm snapshots [--delete ID ...] [--prune]` – list or manage filesystem
  snapshots.
- `lpm rollback [SNAPSHOT_ID]` – restore a snapshot (defaults to latest).
- `lpm history` – show recent transactions.

### Pins and protected packages

- `lpm pins ACTION [NAMES ...] [--prefs name:constraint ...]` – manage holds
  and preferred versions.
- `lpm protected ACTION [NAMES ...]` – view or edit the list of packages that
  cannot be removed unless `--force` is supplied.

### Building packages and repositories

- `lpm build STAGEDIR --name NAME --version VERSION [--release N] [--arch ARCH]
  [--summary TEXT] [--url URL] [--license LICENSE] [--requires PKG ...]
  [--provides PKG ...] [--conflicts PKG ...] [--obsoletes PKG ...]
  [--recommends PKG ...] [--suggests PKG ...] [--output FILE] [--no-sign]`
  – build a `.zst` package from a staged root.
- `lpm splitpkg --stagedir DIR [--name NAME] [--version VERSION] [--release N]`
  `[--arch ARCH] [--summary TEXT] [--requires PKG ...] [--provides PKG ...]`
  `[--conflicts PKG ...] [--obsoletes PKG ...] [--recommends PKG ...]`
  `[--suggests PKG ...] [--outdir DIR] [--output FILE] [--no-sign]` – package an
  additional staged root (for split packages) using the metadata gathered from
  the parent `.lpmbuild`.
- `lpm buildpkg SCRIPT [--outdir PATH] [--no-deps]` – run a `.lpmbuild` script to
  produce a package.
- `lpm pkgbuild-export-tar OUTPUT TARGET... [--workspace DIR]` – developer mode
  helper that fetches Arch Linux PKGBUILDs, converts them to `.lpmbuild`
  scripts (including dependencies), stages them under `packages/<name>` and
  writes the result to an archive at `OUTPUT`. `TARGET` entries can be package
  names such as `extra/zstd`, `repo:core` to pull every package from a
  repository, or paths/URLs to repository `index.json` files.
- `lpm genindex REPO_DIR [--base-url URL] [--arch ARCH]` – generate an
  `index.json` for a directory of packages.
- `lpm installpkg FILE... [--root PATH] [--dry-run] [--verify] [--force]`
  – install from local package files.
- `lpm removepkg NAME... [--root PATH] [--dry-run] [--force]` – remove installed
  packages by name.

#### Exporting Arch PKGBUILDs

When `LPM_DEVELOPER_MODE=1` the `pkgbuild-export-tar` command can bootstrap a
local `.lpmbuild` workspace straight from Arch Linux packaging sources. Provide
one or more package names (optionally prefixed with their repository) or
existing repository `index.json` files and an output archive path:

```
LPM_DEVELOPER_MODE=1 lpm pkgbuild-export-tar arch-export.tar foo extra/zstd repo/index.json
```

LPM fetches each PKGBUILD from `gitlab.archlinux.org`, converts it to
`.lpmbuild`, resolves meta-package dependencies through the same converter, and
stages the results under `packages/<name>/<name>.lpmbuild` before writing the
tarball. The optional `--workspace DIR` flag reuses a conversion cache so
subsequent exports only download new packages. Targets prefixed with
`repo:` (for example `repo:extra`) expand to every package listed in the
upstream repository metadata.

#### Symlink manifest digests

`collect_manifest()` records symbolic links with a `"link"` field describing
their target and stores a SHA‑256 digest alongside each entry.  Packages may use
either of the following formats:

- The default produced by `collect_manifest()` hashes the link target string
  itself.  This keeps manifests stable even when the link points outside the
  staged root.
- Traditional manifests for projects such as glibc store the hash of the file
  payload referenced by the link (for example `/usr/bin/ld.so` matching the
  digest of the loader binary).

`installpkg` recognises both schemes.  When the manifest omits the `"link"`
metadata or when the recorded digest matches the resolved file content, LPM
falls back to hashing the extracted payload so that older packages remain
compatible.

### System bootstrap

- `lpm bootstrap ROOT [--include PKG ...] [--no-verify]` – create a minimal
  chroot populated with packages.

## First run configuration

When `/etc/lpm/lpm.conf` does not exist, `lpm` launches an interactive wizard
before executing the requested command. The wizard displays build metadata from
`get_runtime_metadata()`, the detected init system, and CPU tuning information
derived from the automatic hardware probe. Users can accept the suggested
values or provide alternatives for key settings such as `ARCH`, init policy,
default install answers, fallback download policy, and optional CPU overrides.
The chosen values are written using `save_conf()`, and you can rerun the wizard
at any time with `lpm setup`.

## Optimisation

`lpm` can optimise builds based on your CPU and the selected optimisation
level. The `/etc/lpm/lpm.conf` file accepts an `OPT_LEVEL` entry (`-Os`, `-O2`,
`-O3`, or `-Ofast`). During package builds the manager detects the CPU family
and automatically sets `-march`/`-mtune` along with `-pipe` and `-fPIC` plus the
configured optimisation level for `CFLAGS` and `CXXFLAGS` while `LDFLAGS` uses
only the optimisation level. Any `CFLAGS` defined in a `.lpmbuild` script are
appended to the defaults.

CPU detection can be overridden by specifying `CPU_TYPE` in `lpm.conf`. Set it
to one of `x86_64v1`, `x86_64v2`, `x86_64v3` or `x86_64v4` (underscores or
dashes are accepted) to force the corresponding `-march`/`-mtune` values and
build for a generic target regardless of the host CPU. A per-package override
is also available:

```
!Override @CPU_TYPE="x86_64v2"
```

`lpm` validates this setting; an unrecognized `CPU_TYPE` triggers a warning and
falls back to auto-detected CPU settings. When `CPU_TYPE` is omitted and the CPU
cannot be matched to a known level, the generic target is used.

For Intel processors the detection now inspects the `model` and `flags` fields
from `/proc/cpuinfo`, mapping common family 6 CPUs to GCC's
`x86-64` micro-architecture levels such as `x86-64-v2`, `x86-64-v3` and
`x86-64-v4`.

## Snapshots

LPM stores filesystem snapshots in `/var/lib/lpm/snapshots`. Configure
`MAX_SNAPSHOTS` in `/etc/lpm/lpm.conf` to limit how many snapshots are kept
(default `10`). Older entries beyond the limit are automatically pruned after
creating a new snapshot. You can trigger cleanup manually with
`lpm snapshots --prune`.

By default hardened installations disable the GitLab fallback that fetches
`.lpmbuild` scripts when a repository download fails. Set
`ALLOW_LPMBUILD_FALLBACK=true` in `/etc/lpm/lpm.conf` to re-enable this
behaviour globally. You can override the setting per invocation using
`lpm install ... --allow-fallback` or `--no-fallback`, and the same switches on
`lpm upgrade`.

## Bootstrap

Run `lpm bootstrap /path/to/root --include vim openssh` to create a
chroot‑ready filesystem tree with verified packages.

## Hooks

Hook scripts placed in `/usr/share/lpm/hooks` or within `<hook>.d` directories
run at key points during package operations. These hooks may be either shell or
Python scripts, with Python hooks executed using the current Python interpreter.

### Default post-install hooks

LPM ships with several hooks that run after a package is installed:

- `010-ldconfig.py` – runs `ldconfig` when installing into the real root (`/`).
- `020-update-desktop-database.py` – refreshes the desktop file database if
  `update-desktop-database` is available.
- `030-update-icon-cache.py` – updates icon caches for themes in
  `/usr/share/icons` using `gtk-update-icon-cache`.

Each hook checks for the presence of its corresponding tool and exits quietly
if it is not installed.

### Kernel installation hook

When a package flagged as a kernel is installed, LPM invokes the `kernel_install`
hook. The default Python implementation regenerates the initramfs using
`mkinitcpio` and, if available, updates bootloader entries via `bootctl` or
`grub-mkconfig`.

## Solver heuristics

The resolver uses a CDCL SAT solver with VSIDS‑style variable scoring and phase
saving. Variable and clause activity decay factors default to `0.95` and
`0.999` respectively, tuned from benchmarks on common dependency sets. Package
repositories can influence decision making by adding `"bias"` and `"decay"`
fields to entries in `repos.json`.

A small benchmark harness is provided at `benchmarks/solver_bench.py`. Run
`python benchmarks/solver_bench.py` to measure resolution speed with the
default tuning.

## SAT solver API

The SAT solver can be reused across multiple solves while retaining learned
clauses and variable activity. Instantiate `CDCLSolver` with a `CNF` instance
and call `solve()` with an optional list of assumed literals:

```python
from src import CNF, CDCLSolver

cnf = CNF()
v1 = cnf.new_var("A")
v2 = cnf.new_var("B")
cnf.add_clause([v1, v2])
cnf.add_clause([-v1, v2])

solver = CDCLSolver(cnf)
result = solver.solve([])          # solve normally
result_with_assump = solver.solve([v1])  # assume A is true temporarily
```

Subsequent calls to `solve()` reuse variable activity and learned clauses
accumulated from previous runs, enabling efficient incremental solving.

