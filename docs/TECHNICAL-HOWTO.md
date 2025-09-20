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
and policy defaults.【F:lpm.py†L3027-L3037】 The wizard also runs automatically if
no configuration file exists and you invoke any other command, ensuring that the
package manager never operates without explicit settings.【F:lpm.py†L3194-L3201】
Re-run `lpm setup` whenever you need to change system-wide defaults such as the
CPU tuning level or fallback policy.

### 1.2 Configuration Files and Paths

Key configuration files and directories include:

* `/etc/lpm/lpm.conf` – global defaults consumed from `src.config.CONF`.
* `/etc/lpm/protected.json` – list of packages that cannot be removed without
  `--force`; managed through `lpm protected` (see section 8.2).【F:lpm.py†L69-L119】
* `${XDG_CACHE_HOME:-~/.cache}/lpm` – blob cache cleared by `lpm clean`.
* `/var/lib/lpm/{state.db,cache,snapshots}` – directories initialised on startup
  and consumed by package transactions.【F:src/config.py†L5-L33】【F:lpm.py†L447-L546】

Always ensure these locations are writable inside the root you target; otherwise
commands that modify system state will fail.

## 2. Repository Management

Repositories supply package metadata (`index.json`) and binary blobs. LPM keeps
its repository list in `repos.json` under `/etc/lpm/` and exposes the following
commands for maintenance.

### 2.1 `lpm repolist`

Displays all configured repositories sorted by priority using `list_repos()`.
Each line prints the repository name, its base URL, and numeric priority.【F:lpm.py†L1982-L1995】
Example:

```bash
$ lpm repolist
core            https://repo.example.com/core (prio 5)
extra           https://repo.example.com/extra (prio 10)
```

### 2.2 `lpm repoadd`

Adds a repository definition (name, URL, optional priority) via `add_repo()`.
Priorities are integers where lower numbers win tie-breaks during dependency
resolution.【F:lpm.py†L414-L445】

```bash
# Add a high-priority internal repository
$ sudo lpm repoadd staging https://repo.example.com/staging --priority 2
```

### 2.3 `lpm repodel`

Removes a repository by delegating to `del_repo()`. The repository entry is
removed from the configuration so future resolves ignore it.【F:lpm.py†L420-L426】

```bash
$ sudo lpm repodel staging
```

### 2.4 `lpm clean`

Purges cached blobs from `CACHE_DIR`, freeing local storage. The command removes
both directories and individual files, then reports success.【F:lpm.py†L2656-L2670】
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
`*` (everything).【F:lpm.py†L1989-L2006】

```bash
$ lpm search openssl
openssl                       3.3.1     TLS/SSL cryptography library
```

### 3.2 `lpm info NAME ...`

For each package, `lpm info` shows full metadata including provides, conflicts,
and optional dependencies by reading repository entries.【F:lpm.py†L2008-L2023】

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
for each entry, allowing you to audit what is currently deployed.【F:lpm.py†L2236-L2240】

```bash
$ lpm list | head
bash                           5.2.32-1.x86_64
ca-certificates                2024.07-1.noarch
```

### 3.4 `lpm files NAME`

Lists every file path recorded in the manifest for an installed package. Useful
for locating configuration files or verifying what a package owns.【F:lpm.py†L2208-L2234】

```bash
$ lpm files openssl | grep bin
/usr/bin/openssl
```

## 4. Installing Software

### 4.1 `lpm install`

Resolves dependencies via `solve()`, downloads missing blobs, verifies
signatures (unless `--no-verify`), creates a filesystem snapshot if possible,
and finally installs packages into the chosen root.【F:lpm.py†L2018-L2116】
Key options:

* `--root PATH` – operate inside an alternate root.
* `--dry-run` – print the planned transaction but skip modifications.
* `--allow-fallback` / `--no-fallback` – override the global
  `ALLOW_LPMBUILD_FALLBACK` behaviour for GitLab-based script retrievals.【F:lpm.py†L2059-L2076】
* `--force` – install packages even if they appear in the protected list.

Example:

```bash
# Install vim without modifying protected packages
$ sudo lpm install vim
```

### 4.2 `lpm installpkg FILE ...`

Installs local `.zst` archives directly. Each file is validated (extension,
magic number, signature if `--verify`), metadata is read, and the package is
installed through the same pipeline used by repository installs.【F:lpm.py†L2672-L2767】
Options include `--root`, `--dry-run`, `--verify`, and `--force`.

```bash
$ sudo lpm installpkg ./builds/hello-1.0-1.x86_64.zst --verify
```

## 5. Removing Software

### 5.1 `lpm remove NAME ...`

Uninstalls packages while honouring protected-package rules. Before modifying
files, LPM snapshots the affected paths (unless `--dry-run`) so you can roll
back if necessary.【F:lpm.py†L2110-L2160】 Use `--force` to override protection.

```bash
$ sudo lpm remove oldpkg --force
```

### 5.2 `lpm autoremove`

Computes and removes orphaned dependencies that are no longer required by any
explicitly installed package. Supports `--root` and `--dry-run` just like
`remove`.【F:lpm.py†L2162-L2166】

```bash
$ sudo lpm autoremove
```

### 5.3 `lpm removepkg NAME ...`

Removes already-installed packages by name without consulting repositories,
primarily for local `.zst` deployments. It runs in parallel for efficiency and
accepts `--root`, `--dry-run`, and `--force`.【F:lpm.py†L2600-L2648】

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
order.【F:lpm.py†L2168-L2257】

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
prunes them. Use `--delete` with IDs or `--prune` to enforce `MAX_SNAPSHOTS`.【F:lpm.py†L2242-L2294】

```bash
$ lpm snapshots
   12 2024-08-01 10:22:18 install-vim /var/lib/lpm/snapshots/...
$ sudo lpm snapshots --delete 12
```

### 7.2 `lpm rollback [SNAPSHOT_ID]`

Restores a snapshot archive into the root filesystem. With no argument it picks
the most recent snapshot. The action is logged in history for auditing.【F:lpm.py†L2296-L2316】

```bash
$ sudo lpm rollback 11
```

### 7.3 `lpm history`

Displays the last 200 transactions recorded in the history table, identifying
installs, removals, and rollbacks with timestamps.【F:lpm.py†L2318-L2333】

```bash
$ lpm history | head
2024-08-01 10:22:18  install  vim -> 9.0.1234-1.x86_64
```

## 8. Integrity and Policy Controls

### 8.1 `lpm verify`

Recomputes the manifest for every installed package and reports missing files,
size mismatches, or hash mismatches. Parallel verification keeps large systems
fast. A successful run prints `[OK] All files validated successfully`.【F:lpm.py†L2335-L2369】

```bash
$ sudo lpm verify
```

### 8.2 `lpm pins`

Manipulates `pins.json`, allowing you to hold packages or prefer specific
versions. Actions include `list`, `hold`, `unhold`, and `prefer name:constraint`.
Internally the command updates the JSON file used by the resolver.【F:lpm.py†L2371-L2392】

```bash
$ lpm pins hold openssl zlib
$ lpm pins prefer openssl:~=3.3
```

### 8.3 `lpm protected`

Views or edits the protected package list stored in `protected.json`. The `add`
and `remove` actions mutate the JSON file and emit success messages, while
`list` prints the current contents.【F:lpm.py†L3016-L3034】

```bash
$ sudo lpm protected add kernel linux-firmware
$ lpm protected list
{
  "protected": ["glibc", "kernel", "linux-firmware", "lpm", "zlib"]
}
```

## 9. System Bootstrap

### 9.1 `lpm bootstrap ROOT`

Creates a minimal chroot rooted at `ROOT`. The command ensures essential
directories exist, resolves the package set `lpm-base` and `lpm-core` (plus any
`--include` extras), installs them into the new root, and copies
`/etc/resolv.conf` for network access. Use `--no-verify` if signatures are
unavailable (not recommended).【F:lpm.py†L2073-L2108】

```bash
$ sudo lpm bootstrap /srv/chroot --include openssh vim
```

## 10. Building Packages and Repositories

### 10.1 `lpm build`

Packages a staged filesystem tree (`DESTDIR`) into a `.zst` archive. You must
supply metadata such as `--name`, `--version`, and optionally dependency lists
(`--requires`, `--provides`, etc.). LPM signs the package unless `--no-sign` is
passed, then optionally prompts for installation.【F:lpm.py†L2394-L2411】

```bash
$ lpm build pkgroot --name hello --version 1.0 --arch x86_64 --summary "Hello CLI" \
      --requires glibc --output dist/hello-1.0-1.x86_64.zst
```

### 10.2 `lpm buildpkg`

Executes a `.lpmbuild` script inside a sandbox, running the `prepare`, `build`,
and `install` phases while applying CPU-specific optimisation flags. Dependencies
can be pulled automatically unless `--no-deps` is supplied. The command prints a
Meson-style summary with build time and dependency count when it finishes.【F:lpm.py†L1789-L1890】【F:lpm.py†L2398-L2438】

```bash
$ lpm buildpkg packages/hello/hello.lpmbuild --outdir dist
```

### 10.3 `lpm genindex`

Generates an `index.json` for a repository directory full of `.zst` archives.
You can set a `--base-url` to embed download URLs and restrict output to a
specific `--arch`. Useful for publishing custom repositories.【F:lpm.py†L2652-L2654】

```bash
$ lpm genindex repo/ --base-url https://repo.example.com/custom
```

## 11. Troubleshooting Tips

* Use `--dry-run` with install/remove/upgrade to inspect resolver output before
  committing changes.
* Keep an eye on `lpm history` and `lpm snapshots` so you always have a rollback
  path after large upgrades.
* If verification fails (`lpm verify`), compare reported mismatches against the
  corresponding manifest entries to detect tampering or manual edits.
* Periodically run `lpm clean` to trim caches, especially on build servers.

With these commands and workflows, you can confidently operate LPM across
production systems, chroots, and custom repositories.
