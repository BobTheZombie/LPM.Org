# LPM Technical How-To

This guide walks through every end-user command exposed by `lpm`, explains what
happens under the hood, and illustrates real-world usage patterns. It is written
for administrators who want to understand how LPM manipulates repositories,
resolves dependencies, manages system snapshots, and builds distributable
packages. Developer-mode-only helper commands (such as Arch PKGBUILD
converters) are intentionally excluded.

Throughout the guide:

* **HOST ROOT** refers to the live filesystem (default `/`).
* **ALT ROOT** refers to an alternate install root supplied with `--root`, such
  as a chroot or staging directory.
* Paths like `/etc/lpm/lpm.conf` always refer to the target root. When you run
  commands against `--root /tmp/chroot`, the effective configuration file becomes
  `/tmp/chroot/etc/lpm/lpm.conf`.

All commands are invoked as `lpm SUBCOMMAND [OPTIONS]`. Use `lpm SUBCOMMAND -h`
for concise argument help at any time.

## 1. First-Run Experience and Configuration

### 1.1 `lpm setup`

`lpm setup` launches the interactive wizard provided by `run_first_run_wizard()`
which populates `/etc/lpm/lpm.conf` with architecture, optimisation, repository,
and policy defaults.【F:src/lpm/app.py†L5767-L5768】 The wizard also runs automatically if
no configuration file exists and you invoke any other command, ensuring that the
package manager never operates without explicit settings.【F:src/lpm/app.py†L6139-L6143】
Re-run `lpm setup` whenever you need to change system-wide defaults such as the
CPU tuning level or fallback policy.

### 1.2 Configuration Files and Paths

Key configuration files and directories include:

* `/etc/lpm/lpm.conf` – global defaults consumed from `src.config.CONF`.
* `/etc/lpm/protected.json` – list of packages that cannot be removed without
  `--force`; managed through `lpm protected` (see section 8.2).【F:src/lpm/app.py†L205-L222】【F:src/lpm/app.py†L5741-L5763】
* `${XDG_CACHE_HOME:-~/.cache}/lpm` – blob cache cleared by `lpm clean`.
* `/var/lib/lpm/{state.db,cache,snapshots}` – directories initialised on startup
  and consumed by package transactions.【F:src/config.py†L63-L90】

High-throughput environments can raise the downloader pool and decompression
buffer directly from `lpm.conf`. `FETCH_MAX_WORKERS` controls how many blob
downloads run in parallel (defaulting to twice the CPU core count, clamped
between 4 and 32), while `IO_BUFFER_SIZE` sets the non-streaming extraction
buffer in bytes (default 1 MiB, minimum 64 KiB).【F:src/config.py†L43-L55】【F:src/config.py†L203-L213】【F:src/lpm/app.py†L1724-L1733】【F:src/lpm/app.py†L1939-L1958】

Always ensure these locations are writable inside the root you target; otherwise
commands that modify system state will fail.

## 2. Repository Management

Repositories supply package metadata (`index.json`) and binary blobs. LPM keeps
its repository list in `repos.json` under `/etc/lpm/` and exposes the following
commands for maintenance.

### 2.1 `lpm repolist`

Displays all configured repositories sorted by priority using `list_repos()`.
Each line prints the repository name, its base URL, and numeric priority.【F:src/lpm/app.py†L4407-L4409】
Example:

```bash
$ lpm repolist
core            https://repo.example.com/core (prio 5)
extra           https://repo.example.com/extra (prio 10)
```

### 2.2 `lpm repoadd`

Adds a repository definition (name, URL, optional priority) via `add_repo()`.
Priorities are integers where lower numbers win tie-breaks during dependency
resolution.【F:src/lpm/app.py†L692-L718】

```bash
# Add a high-priority internal repository
$ sudo lpm repoadd staging https://repo.example.com/staging --priority 2
```

### 2.3 `lpm repodel`

Removes a repository by delegating to `del_repo()`. The repository entry is
removed from the configuration so future resolves ignore it.【F:src/lpm/app.py†L697-L698】

```bash
$ sudo lpm repodel staging
```

### 2.4 `lpm clean`

Purges cached blobs from `CACHE_DIR`, freeing local storage. The command removes
both directories and individual files, then reports success.【F:src/lpm/app.py†L5090-L5098】
Run it when you need to reclaim disk space or after switching mirrors.

```bash
$ sudo lpm clean
[OK] Removed cached blobs
```

## 3. Discovering Packages

These commands read repository metadata to help you find packages before you
modify the system.

### 3.1 `lpm search [PATTERN ...]`

Loads the entire package universe (`load_universe()`) and prints matching names,
versions, and summaries. Shell-style wildcards are allowed; no pattern means
`*` (everything).【F:src/lpm/app.py†L706-L718】【F:src/lpm/app.py†L4414-L4422】

```bash
$ lpm search openssl
openssl                       3.3.1     TLS/SSL cryptography library
```

### 3.2 `lpm info NAME ...`

For each package, `lpm info` shows full metadata including provides, conflicts,
and optional dependencies by reading repository entries.【F:src/lpm/app.py†L4424-L4442】

```bash
$ lpm info openssl
Name:       openssl
Version:    3.3.1-1.x86_64
Summary:    TLS/SSL cryptography library
Homepage:   https://www.openssl.org/
License:    Apache-2.0
Provides:   libcrypto.so, libssl.so
Requires:   zlib
Conflicts:  libressl
Obsoletes:  -
Recommends: -
Suggests:   -
Blob:       openssl-3.3.1-1.x86_64.zst
```

### 3.3 `lpm list`

Queries the installed-package database and prints `name version-release.arch`
for each entry, allowing you to audit what is currently deployed.【F:src/lpm/app.py†L4661-L4685】

```bash
$ lpm list | head
bash                           5.2.32-1.x86_64
ca-certificates                2024.07-1.noarch
```

### 3.4 `lpm files NAME`

Lists every file path recorded in the manifest for an installed package. Useful
for locating configuration files or verifying what a package owns.【F:src/lpm/app.py†L4640-L4650】

```bash
$ lpm files openssl | grep bin
/usr/bin/openssl
```

## 4. Installing Software

### 4.1 `lpm install`

Resolves dependencies via `solve()`, downloads missing blobs, verifies
signatures (unless `--no-verify`), creates a filesystem snapshot if possible,
and finally installs packages into the chosen root.【F:src/lpm/app.py†L4444-L4494】
Key options:

* `--root PATH` – operate inside an alternate root.
* `--dry-run` – print the planned transaction but skip modifications.
* `--allow-fallback` / `--no-fallback` – override the global
  `ALLOW_LPMBUILD_FALLBACK` behaviour for GitLab-based script retrievals.【F:src/lpm/app.py†L4459-L4461】
* `--force` – install packages even if they appear in the protected list.

Example:

```bash
# Install vim without modifying protected packages
$ sudo lpm install vim
```

### 4.2 `lpm installpkg FILE ...`

Installs local `.zst` archives directly. Each file is validated (extension,
magic number, signature if `--verify`), metadata is read, and the package is
installed through the same pipeline used by repository installs.【F:src/lpm/app.py†L5230-L5286】
Options include `--root`, `--dry-run`, `--verify`, and `--force`.

```bash
$ sudo lpm installpkg ./builds/hello-1.0-1.x86_64.zst --verify
```

## 5. Removing Software

### 5.1 `lpm remove NAME ...`

Uninstalls packages while honouring protected-package rules. Before modifying
files, LPM snapshots the affected paths (unless `--dry-run`) so you can roll
back if necessary.【F:src/lpm/app.py†L4501-L4522】 Use `--force` to override protection.

```bash
$ sudo lpm remove oldpkg --force
```

### 5.2 `lpm autoremove`

Computes and removes orphaned dependencies that are no longer required by any
explicitly installed package. Supports `--root` and `--dry-run` just like
`remove`.【F:src/lpm/app.py†L4531-L4533】

All package transactions automatically trigger the system-maintenance hook,
which runs `lpm autoremove --root "$LPM_ROOT"` plus snapshot pruning and cache
cleaning for the real root, keeping orphaned packages and stale data from
accumulating between manual maintenance sessions.【F:usr/libexec/lpm/hooks/system-maintenance†L1-L49】【F:usr/share/liblpm/hooks/system-maintenance.hook†L1-L10】

```bash
$ sudo lpm autoremove
```

### 5.3 `lpm removepkg NAME ...`

Removes already-installed packages by name without consulting repositories,
primarily for local `.zst` deployments. It runs in parallel for efficiency and
accepts `--root`, `--dry-run`, and `--force`.【F:src/lpm/app.py†L5101-L5114】【F:src/lpm/app.py†L6115-L6120】

```bash
$ sudo lpm removepkg hello --dry-run
```

## 6. Upgrading Systems

### 6.1 `lpm upgrade [NAME ...]`

When invoked without package names, `lpm upgrade` refreshes every installed
package to the latest available version by generating version constraints such as
`pkg ~= current_version`. With explicit names it only targets the listed
packages. The command honours verification and fallback flags, creates
snapshots, removes obsolete service files, and upgrades packages in dependency
order.【F:src/lpm/app.py†L4535-L4634】

```bash
# Upgrade everything with verification
$ sudo lpm upgrade

# Upgrade only openssl and curl, allowing fallback script fetches
$ sudo lpm upgrade openssl curl --allow-fallback
```

## 7. Snapshotting and History

LPM automatically snapshots affected files during install/remove/upgrade
operations (when not run in dry-run mode). These commands help you monitor and
recover from changes.

### 7.1 `lpm snapshots`

Lists stored snapshots (`id timestamp tag archive`) and optionally deletes or
prunes them. Use `--delete` with IDs or `--prune` to enforce `MAX_SNAPSHOTS`.【F:src/lpm/app.py†L4707-L4733】

```bash
$ lpm snapshots
   12 2024-08-01 10:22:18 install-vim /var/lib/lpm/snapshots/...
$ sudo lpm snapshots --delete 12
```

### 7.2 `lpm rollback [SNAPSHOT_ID]`

Restores a snapshot archive into the root filesystem. With no argument it picks
the most recent snapshot. The action is logged in history for auditing.【F:src/lpm/app.py†L4734-L4752】

```bash
$ sudo lpm rollback 11
```

### 7.3 `lpm history`

Displays the last 200 transactions recorded in the history table, identifying
installs, removals, and rollbacks with timestamps.【F:src/lpm/app.py†L4753-L4763】

```bash
$ lpm history | head
2024-08-01 10:22:18  install  vim -> 9.0.1234-1.x86_64
```

## 8. Integrity and Policy Controls

### 8.1 `lpm verify`

Recomputes the manifest for every installed package and reports missing files,
size mismatches, or hash mismatches. Parallel verification keeps large systems
fast. A successful run prints `[OK] All files validated successfully`.【F:src/lpm/app.py†L4766-L4806】

```bash
$ sudo lpm verify
```

### 8.2 `lpm pins`

Manipulates `pins.json`, allowing you to hold packages or prefer specific
versions. Actions include `list`, `hold`, `unhold`, and `prefer name:constraint`.
Internally the command updates the JSON file used by the resolver.【F:src/lpm/app.py†L4809-L4833】

```bash
$ lpm pins hold openssl zlib
$ lpm pins prefer openssl:~=3.3
```

### 8.3 `lpm protected`

Views or edits the protected package list stored in `protected.json`. The `add`
and `remove` actions mutate the JSON file and emit success messages, while
`list` prints the current contents.【F:src/lpm/app.py†L5741-L5763】

```bash
$ sudo lpm protected add kernel linux-firmware
$ lpm protected list
{
  "protected": ["glibc", "kernel", "linux-firmware", "lpm", "zlib"]
}
```

## 9. Building Packages and Repositories

### 9.1 `lpm build`

Packages a staged filesystem tree (`DESTDIR`) into a `.zst` archive. You must
supply metadata such as `--name`, `--version`, and optionally dependency lists
(`--requires`, `--provides`, etc.). LPM signs the package unless `--no-sign` is
passed, then optionally prompts for installation.【F:src/lpm/app.py†L4836-L4847】

```bash
$ lpm build pkgroot --name hello --version 1.0 --arch x86_64 --summary "Hello CLI" \
      --requires glibc --output dist/hello-1.0-1.x86_64.zst
```

### 9.2 `lpm buildpkg`

Executes a `.lpmbuild` script inside a sandbox, running the `prepare`, `build`,
and `install` phases while applying CPU-specific optimisation flags. Dependencies
can be pulled automatically unless `--no-deps` is supplied. The command prints a
Meson-style summary with build time and dependency count when it finishes.【F:src/lpm/app.py†L3499-L3570】【F:src/lpm/app.py†L4010-L4041】【F:src/lpm/app.py†L4213-L4243】【F:src/lpm/app.py†L5021-L5078】

```bash
$ lpm buildpkg packages/hello/hello.lpmbuild --outdir dist
```

During the `install` phase the script can emit extra packages by calling the
`LPM_SPLIT_PACKAGE` helper. The environment variable points to a tiny wrapper
around `lpm splitpkg` that reuses the metadata collected from the parent
package. Supply a staged root and any metadata overrides to produce a
fully-signed package inline, for example:

```bash
splitdir="$BUILDROOT/gcc-fortran"
mkdir -p "$splitdir/usr/bin"
cp fortran/* "$splitdir/usr/bin/"
"$LPM_SPLIT_PACKAGE" --stagedir "$splitdir" --name gcc-fortran --requires gcc-libs
```

Each invocation writes the package to the current build output directory and is
reported alongside the primary package when the build completes.【F:src/lpm/app.py†L5075-L5081】

### 9.3 `lpm genindex`

Generates an `index.json` for a repository directory full of `.zst` archives.
You can set a `--base-url` to embed download URLs and restrict output to a
specific `--arch`. Useful for publishing custom repositories.【F:src/lpm/app.py†L2867-L2894】

```bash
$ lpm genindex repo/ --base-url https://repo.example.com/custom
```

### 9.4 Distribution maintainer mode workflow

Turn on maintainer mode when you want LPM to publish packages automatically.
Run `lpm setup`, answer **yes** to the maintainer prompt, and provide the
repository/source locations and optional Git details when prompted. The wizard
persists the answers to `/etc/lpm/lpm.conf` as `DISTRO_*` keys, which you can
also edit manually if you prefer.【F:src/first_run_ui.py†L123-L194】【F:src/config.py†L24-L87】【F:src/config.py†L210-L233】

When maintainer mode is active, every call to `lpm buildpkg` (and the internal
builders used by `lpm pip build`) copies the main package, split packages, and
their detached signatures into `<DISTRO_REPO_ROOT>/<arch>/`, archives sources
under `DISTRO_SOURCE_ROOT`, and records metadata plus the `.lpmbuild` script
under `DISTRO_LPMBUILD_ROOT`. LPM then regenerates `index.json` files so the
repository stays installable without extra steps.【F:src/maintainer_mode.py†L100-L210】【F:src/lpm/app.py†L1589-L1626】【F:src/lpm/app.py†L3210-L3236】

Enable Git automation to have LPM stage the new and updated files, commit them
with a summary derived from the published artifacts, and push to a configured
remote/branch. Leaving the remote blank keeps the changes local for manual
review or alternative deployment tools.【F:src/maintainer_mode.py†L214-L274】

## 10. Troubleshooting Tips

* Use `--dry-run` with install/remove/upgrade to inspect resolver output before
  committing changes.
* Keep an eye on `lpm history` and `lpm snapshots` so you always have a rollback
  path after large upgrades.
* If verification fails (`lpm verify`), compare reported mismatches against the
  corresponding manifest entries to detect tampering or manual edits.
* Periodically run `lpm clean` to trim caches, especially on build servers.

With these commands and workflows, you can confidently operate LPM across
production systems, chroots, and custom repositories.
