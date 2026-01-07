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
- **Global transaction locking** via `/var/lib/lpm/lock` so only one package
  operation runs at a time, complementing snapshot rollbacks.
- **Bootstrap mode** to build a minimal chroot and populate it with verified
  packages.
- **Automated maintenance hooks** that run `lpm autoremove`, prune snapshots,
  and clean the cache after each transaction to keep systems tidy.【F:usr/libexec/lpm/hooks/system-maintenance†L1-L49】【F:usr/share/liblpm/hooks/system-maintenance.hook†L1-L10】
- **Init system awareness** that detects packaged systemd/OpenRC/SysV units and
  can auto-enable them when allowed; core init packages (e.g., `systemd` and
  `systemd-libs`) are deliberately excluded from automatic unit management.
- **Incremental SAT solver API** available for other tools and benchmarks in
  `benchmarks/solver_bench.py`.
- **.lpmbuild scripts** for reproducible package builds and a `build` command to
  package staged roots.

## Documentation

The new [Technical How-To](docs/TECHNICAL-HOWTO.md) walks through every
end-user command in depth, including configuration, repository management,
package workflows, snapshotting, verification, and package creation. Refer to
it whenever you need detailed examples or flag reference material.

## Building LPM and the graphical UI

The project ships with a `Makefile` that invokes Nuitka to produce stand-alone
executables for both the command-line client and the Qt-based graphical
frontend.

1. Install build dependencies for Nuitka, including `patchelf` on Linux (see
   the notes below for details on static builds).
2. From the repository root run `make` (or `make all`) to build:
   - `build/nuitka/lpm.bin` – the command-line interface.
   - `build/nuitka/lpm-ui.bin` – the graphical launcher that can be run
     separately, similar to using `apt` alongside `synaptic`.

Both binaries are rebuilt on every invocation so you can keep the CLI and GUI in
lockstep. The makefile automatically installs/updates Nuitka from the pinned Git
revision during the build.

### Building distributable wheels

`pyproject.toml` defines the package metadata, console entry points (`lpm` and
`lpm-ui`), and an optional `ui` extra that pulls in the PySide6 dependencies
listed in `requirements-ui.txt`. Run `python -m build` from the repository root
to produce both an sdist and a wheel. By default the wheel is pure Python and
does not include any Nuitka-compiled executables. To opt in to compiled launchers,
install the extra and set `LPM_ENABLE_NUITKA=1`:

```sh
python -m pip install ".[nuitka]"
LPM_ENABLE_NUITKA=1 python -m build
```

You can also invoke Nuitka directly with `python setup.py build_nuitka` when you
only want the compiled artifacts.

Nuitka compilation is available via custom setuptools commands. The following
environment variables tweak the process when invoking `python -m build` or
`python setup.py build_nuitka`:

- `LPM_ENABLE_NUITKA=1` – opt in to running Nuitka during `python -m build`.
- `LPM_SKIP_NUITKA=1` – omit the compilation step entirely (useful when running
  `build_nuitka` but wanting to skip it conditionally).
- `LPM_NUITKA_FLAGS="..."` – append additional flags for both the CLI and UI
  builds.
- `LPM_NUITKA_UI_FLAGS="..."` – supply UI-specific flags (defaults to
  `--enable-plugin=pyside6`).
- `LPM_NUITKA_BIN="python -m nuitka"` – override the command used to run Nuitka.
- `LPM_NUITKA_SKIP_UI=1` – skip compiling the GUI launcher (useful when PySide6
  is unavailable).
- `LPM_NUITKA_FORCE_UI=1` – force the GUI build even if PySide6 cannot be
  imported, letting you manage dependencies externally.

### Build prerequisites

On Linux, Nuitka's onefile mode requires [`patchelf`](https://nixos.org/patchelf)
to be available on the build host. Install it with your system package manager
before running `make`; for example, `apt-get install patchelf` on Debian and
Ubuntu derivatives.

### Using a static libpython

Static linking is controlled by the `STATIC_LIBPYTHON` make variable:

```sh
# Possible values: auto (default), yes, or no
make STATIC_LIBPYTHON=yes
```

- `auto` attempts to locate a `libpython*.a` archive for the selected Python
  interpreter and enables Nuitka's `--static-libpython` flag when the archive
  is present.
- `yes` requires the archive to exist; the build aborts if it cannot be
  located.
- `no` always uses the shared `libpython`.

When `STATIC_LIBPYTHON=yes` the build system rewrites CPython's
`Modules/Setup.local` so that every bundled extension is compiled into the
static `libpython` archive. This guarantees Nuitka can resolve the modules at
runtime without shipping the accompanying `.so` files alongside the binary.

The helper toolchain no longer forces `LDFLAGS=-static` during CPython's
configure step.  If you require a fully static interpreter you can provide
custom flags, for example:

```sh
make STATIC_LIBPYTHON=yes \
     STATIC_PYTHON_ENV='LDFLAGS="-static" CPPFLAGS="" CFLAGS=""'
```

Leaving `STATIC_PYTHON_ENV` unset lets the upstream build decide how to link
Python, which avoids linker errors while the profile-guided optimization stage
builds temporary shared modules.

Set `PYTHON=/path/to/python` if you need to build with a specific interpreter.
When a static archive is detected the make output includes the file that will
be linked so you can confirm which Python installation is being used.

### Nuitka toolchain status

The `develop` branch of `https://github.com/BobTheZombie/Nuitka.git` at commit
`43502b8cd001214ccf7c040d2f39c336b7221a2e` (tagged `Nuitka-2.8rc16`) remained
the latest upstream revision when checked on 2025-10-08 at 18:44 UTC. Running
`make all` on that date reinstalled Nuitka from the repository and produced
`build/nuitka/lpm.bin`. The Makefile now performs a fresh `pip install` of
Nuitka from the checked-out revision on every build, guaranteeing the embedded
toolchain is recreated even when the upstream commit hash has not changed.
Future updates should re-run the build to refresh this section with the new
commit hash.

## Graphical interface

### Running the PySide6 UI from source

The modern Qt experience lives in `src/ui/qt_app.py`. To launch it directly
from a source checkout:

1. Install the UI dependency once (a virtual environment is recommended):

   ```sh
   python -m pip install -r requirements-ui.txt
   ```

2. Start the interface from the repository root so the package imports
   resolve:

   ```sh
   python -m src.ui.qt_app
   ```

The PySide6 front-end is a thin wrapper over the CLI, so installing or
removing packages still requires the same privileges you would normally grant
`lpm` on the command line.

The `lpm_ui.py` launcher performs the same dependency check and boots the
PySide6 interface; run it with `python lpm_ui.py` if you prefer to execute the
top-level entry point directly.

## Command line interface

`lpm` uses sub‑commands. Each command listed below shows its required
arguments and optional flags.

### System configuration helper

Run `sudo lpm --sysconfig` to have LPM create a baseline set of shell startup files and console defaults for the current root (`/` by default). The helper installs sensible `/etc/profile` and `/etc/bash.bashrc` templates, initialises per-user startup files (`~/.bash_profile`, `~/.bashrc`, `~/.bash_logout`), installs `/etc/DIR_COLORS` and `/etc/vconsole.conf`, prepares `/etc/locale.gen` and `/etc/locale.conf`, and writes `/etc/default/useradd` alongside wrapper scripts that delegate to `useradd` and `adduser` with LPM's defaults. After templating the locale files it runs `locale-gen` (when available) so the locale archive under `/usr/lib/locale/locale-archive` stays in sync. Existing files are preserved – the helper only creates missing paths or appends an annotated block when safe to do so – so you can rerun the command whenever you need to refresh the system configuration after installing LPM.

Pass `--sysconfig-root /some/chroot` to target an alternate filesystem tree, which is useful while provisioning chroots or offline images. No other subcommands may be supplied when `--sysconfig` is present; the helper performs its work and exits after printing a summary of the files it touched.

- **Distribution maintainer mode** – see the dedicated section below for
  automating repository publication after builds.
- `lpm setup` – launch the interactive first-run configuration wizard. The
  wizard also runs automatically the first time `lpm` starts if
  `/etc/lpm/lpm.conf` is missing.
- `lpm clean` – purge cached package blobs from `${XDG_CACHE_HOME:-~/.cache}/lpm`.

### Distribution maintainer mode

Maintainer mode turns LPM into a turnkey release pipeline that copies finished
packages, their detached signatures, sources, and `.lpmbuild` scripts into a
publishable tree every time you invoke `lpm buildpkg`, the Python package
builder, or any other entry point that produces `.zst` artifacts.【F:src/maintainer_mode.py†L100-L210】【F:src/lpm/app.py†L1589-L1626】【F:src/lpm/app.py†L3210-L3236】

1. Run `sudo lpm setup` and answer **yes** when prompted "Enable distribution
   maintainer mode?". The wizard will then ask for the distribution name,
   repository/source roots, and (optionally) Git information. Press Enter to
   accept the suggested defaults under `/var/lib/lpm/maintainer/...` or point
   them at existing storage.【F:src/first_run_ui.py†L123-L194】【F:src/config.py†L24-L87】【F:src/config.py†L210-L233】
2. To enable the pipeline without the wizard, populate `/etc/lpm/lpm.conf` with
   the `DISTRO_*` keys. For example:

   ```ini
   DISTRO_MAINTAINER_MODE=yes
   DISTRO_NAME=MyLPM
   DISTRO_REPO_ROOT=/srv/lpm/repo
   DISTRO_SOURCE_ROOT=/srv/lpm/sources
   DISTRO_LPMBUILD_ROOT=/srv/lpm/lpmbuilds
   DISTRO_REPO_BASE_URL=https://repo.example.com/lpm
   DISTRO_GIT_ENABLED=yes
   DISTRO_GIT_ROOT=/srv/lpm/repo
   DISTRO_GIT_REMOTE=origin
   DISTRO_GIT_BRANCH=main
   ```

   Re-run `lpm setup` at any time to revise these settings. LPM reloads the
   configuration for every command, so the changes take effect immediately.【F:src/config.py†L200-L260】

3. Build packages as usual. When maintainer mode is active, LPM copies each
   artifact (and any split sub-packages) into `<repo-root>/<arch>/`, writes an
   `index.json` using the same generator that powers standard repositories, and
   archives sources plus metadata under the configured roots. Detached
   signatures (`.sig`) ride along automatically.【F:src/maintainer_mode.py†L132-L210】
4. If Git publishing is enabled, LPM stages every new file under the configured
   repository root, commits them with a message such as `MyLPM: publish
   pkg-version-arch`, and pushes to the chosen remote/branch when configured.
   Skip the remote to keep changes local for manual review.【F:src/maintainer_mode.py†L214-L274】

Repositories produced this way are ready to serve over HTTP/HTTPS or export via
`file://` URLs. Pair them with your existing mirror tooling or point other LPM
installations at the generated `index.json` files.

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
- Module catalogs such as
  ```
  usr/lib/gio/modules/libfoo.so
  usr/lib/pkcs11/libfoo.so
  ```
  regenerate via transaction hooks that call `gio-querymodules` and `modutil`
  to keep the runtime caches in sync.

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
- `lpm buildpkg SCRIPT [--outdir PATH] [--no-deps] [--force-rebuild]` – run a `.lpmbuild` script to
  produce a package. `.lpmbuild` scripts may declare a `SOURCE=()` array; entries
  without a URL scheme automatically resolve to
  `{LPMBUILD_REPO}/{pkgname}/{filename}`, matching Arch Linux's `source=()`
  behaviour, while explicit URLs and `foo::https://example.com/src` rename
  syntax are honoured as-is.【F:src/lpm/app.py†L4044-L4092】
  For Python dependencies that should be sourced from PyPI, add a
  `REQUIRES_PYTHON_DEPENDENCIES=()` array with standard pip requirement
  strings such as `('requests==2.0')`; `lpm buildpkg` canonicalises the
  distribution names, skips entries already provided by packages exposing
  `pypi(<name>)`, and otherwise invokes the built-in pip builder (with
  dependency resolution enabled) before executing your script.【F:src/lpm/app.py†L3663-L3721】
  Meta-packages can also map individual dependencies to virtual provides by
  declaring an associative array named `META_PROVIDES` (or the lowercase
  `meta_provides`). Each key corresponds to a required package while the value
  is a space-separated list of capabilities that should be exposed by the
  meta-package. For example:

  ```bash
  declare -A META_PROVIDES=([
    glibc="glibc virtual(libc)"
    filesystem="filesystem"
  ])
  ```

  ensures that `system-base` advertises `glibc` and `virtual(libc)` while still
  depending on the real `glibc` package.【F:src/lpm/app.py†L3628-L3659】
- `lpm genindex REPO_DIR [--base-url URL] [--arch ARCH]` – generate an
  `index.json` for a directory of packages.
- `lpm installpkg FILE... [--root PATH] [--dry-run] [--verify] [--force]`
  – install from local package files.
- `lpm removepkg NAME... [--root PATH] [--dry-run] [--force]` – remove installed
  packages by name.

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
dashes are accepted) to force the corresponding `-march`/`-mtune` values (with
`x86_64v1` mapping to the GCC-compatible `x86-64`) and build for a generic
target regardless of the host CPU. A per-package override is also available:

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

## Hooks

LPM supports two complementary hook systems: transaction-scoped `.hook` files
and the legacy per-package script directories.

### Transaction hooks (`.hook` files)

`liblpmhooks` loads ALPM-compatible hook definitions from the system directory
`/usr/share/liblpm/hooks` and the administrator override directory
`/etc/lpm/hooks`. Each `.hook` file contains one or more `[Trigger]` sections
describing which package names or filesystem paths should activate the hook and
an `[Action]` section describing what to run. Supported keys are:

| Key | Section | Description |
| --- | --- | --- |
| `Type` | `[Trigger]` | `Package` (match package globs) or `Path` (match manifest paths). |
| `Operation` | `[Trigger]` | `Install`, `Upgrade`, or `Remove`. Multiple values are allowed. |
| `Target` | `[Trigger]` | Glob pattern matched against package names or relative paths. |
| `When` | `[Action]` | `PreTransaction` or `PostTransaction`. |
| `Exec` | `[Action]` | Command to execute. |
| `NeedsTargets` | `[Action]` | When present, target values are appended to the command line and exposed via `LPM_TARGETS` / `LPM_TARGET_COUNT`. |
| `Depends` | `[Action]` | Names of other hooks that must run first. |
| `AbortOnFail` | `[Action]` | Abort the transaction if the command exits with a non-zero status. |

Hooks are queued once per transaction. All matching `PreTransaction` hooks run
before any filesystem changes are made, while `PostTransaction` hooks run after
the package set has been applied. `Depends` relationships are resolved within a
phase to ensure deterministic ordering. Commands inherit the standard process
environment along with `LPM_HOOK_NAME`, `LPM_HOOK_WHEN`, and `LPM_ROOT`.

### Legacy script directories

The original script hook mechanism remains available for compatibility. Any
executable placed in `/usr/share/lpm/hooks` or within `<hook>.d` directories runs
at the appropriate per-package lifecycle point. These hooks may be shell or
Python scripts; Python hooks execute with the interpreter used to run `lpm`.

#### Post-install and post-upgrade scripts

LPM ships with several scripts that run after a package is installed:

- `010-ldconfig.py` – runs `ldconfig` when installing into the real root (`/`).
- `020-update-desktop-database.py` – refreshes the desktop file database if
  `update-desktop-database` is available.
- `030-update-icon-cache.py` – updates icon caches for themes in
  `/usr/share/icons` using `gtk-update-icon-cache`.

Each script checks for the presence of its corresponding tool and exits quietly
if it is not installed.

In addition to the per-package `post_install` hook, upgrades trigger a
`post_upgrade` entry point after the new files have been committed. Both hooks
receive the same environment variables:

| Variable | Description |
| --- | --- |
| `LPM_PKG` | Package name being installed or upgraded. |
| `LPM_VERSION` | Version of the package that has just been installed. |
| `LPM_RELEASE` | Release string for the installed package. |
| `LPM_ROOT` | Destination root path passed to `lpm install`. |
| `LPM_PREVIOUS_VERSION` | Previous installed version when upgrading (unset on fresh installs). |
| `LPM_PREVIOUS_RELEASE` | Previous installed release when upgrading (unset on fresh installs). |

To add custom logic, drop an executable into either
`/usr/share/lpm/hooks/post_install.d/` or `/usr/share/lpm/hooks/post_upgrade.d/`.
For example, to notify a service after every upgrade create
`/usr/share/lpm/hooks/post_upgrade.d/900-notify.sh`:

```sh
#!/bin/sh
[ -z "${LPM_PREVIOUS_VERSION:-}" ] && exit 0
logger -t lpm "${LPM_PKG} upgraded from ${LPM_PREVIOUS_VERSION}-${LPM_PREVIOUS_RELEASE} to ${LPM_VERSION}-${LPM_RELEASE}"
systemctl reload my-service.service
```

The script is executed automatically after each upgrade. Similar scripts placed
in `post_install.d/` run after every installation (including upgrades). Script
hooks execute alongside the transaction hooks described above, making it easy to
combine coarse-grained `.hook` automation with fine-grained per-package logic.

### Kernel installation hook

When a kernel package installs files under `/usr/lib/modules/<version>/`, LPM's
transaction hook invokes `kernel_install`. The default implementation runs
`depmod` for each affected version, regenerates
`/boot/initrd-<version>.img` via `mkinitcpio` (respecting any `LPM_PRESET`
override) and, if available, updates bootloader entries via `bootctl` or
`grub-mkconfig`.

Similarly, when packages matching `grub`, `grub2*`, or `grub-efi*` are
installed or upgraded, the `update-grub` transaction hook regenerates
`/boot/grub/grub.cfg` using `update-grub` or `grub-mkconfig` when those tools
are present on the system.

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
