# Persistent buildpkg and kmake state

Buildpkg previously reset its source, build, and staging directories before
running recipe functions. Consequently a recipe-level resume flag could not
protect objects from deletion. Resume is now selected and validated before
those resets, source fetching, extraction, or patching.

## Commands

```sh
# Start a tracked build. Failed builds retain their source and build objects.
lpm buildpkg ./linux-kernel/linux-kernel.lpmbuild --install-default y

# Inspect the saved phases, active/failed phase, directories and package paths.
lpm buildpkg ./linux-kernel/linux-kernel.lpmbuild --status

# Resume, allowing a different compilation job count.
LPM_BUILD_JOBS=6 lpm buildpkg ./linux-kernel/linux-kernel.lpmbuild \
    --resume --no-deps --install-default y

# Explicitly discard an old workspace and re-extract from scratch.
lpm buildpkg ./linux-kernel/linux-kernel.lpmbuild --restart
```

A normal invocation refuses to erase an unfinished tracked workspace. It directs
you to choose `--resume` or `--restart`. `--force-rebuild` controls rebuilding
dependencies; it does not grant permission to discard unfinished work. Combine
it with `--restart` when a destructive fresh build is intended. `--resume` and
`--force-rebuild` cannot be combined.

`--python-pip` builds do not yet implement these checkpoints. They reject the
resume/status/restart switches rather than pretend to support them.

## Checkpoints and validation

The state file is `STATE_DIR/builds/NAME/state.json` (normally beneath
`/var/lib/lpm`). It is separate from the disposable build directories, written
atomically with file and directory fsync, and guarded by a per-package lock.

Saved data includes recipe/local-input hashes, package version/release/arch,
build stage, CPU overrides, effective flags and compiler fingerprint, workspace
paths/device/inode identities, fetched input hashes, completed phases, current
phase, errors, and hashes/metadata of completed package artifacts.

Resume validates recipe/local inputs and existing workspaces before entering
the build pipeline. The effective compiler and flags are checked before source
operations or compilation. Missing/replaced source or build directories,
redirected paths, altered input files, and incompatible flags fail without
cleaning the retained source or build objects. Changing the job count is allowed.
A changed output directory for a completed build does not relocate its existing
package; status reports the original artifact path.

State is not a hash of every file in the extracted working tree. Recipes and
source trees are trusted build inputs; upstream make/Kbuild still tracks edits
and object dependencies. This is not a hermetic/reproducible-build guarantee.

## Phase behavior

- Completed source fetching, extraction, patching, and source hooks are skipped.
- Completed prepare/build/check phases are skipped. A declared `check()` (or its
  stage-2 counterpart) now runs before staging.
- The failed/interrupted compilation phase is rerun so make/Kbuild can reuse
  existing objects. Buildpkg does not reinterpret arbitrary recipe commands.
- Staging is recreated on retry unless a verified completed package can be
  reused. Source/build directories are retained. This avoids stale kernel
  modules and partially installed files.
- A completed package is hash-verified and reused for an installation retry.
  Installation and its transaction hooks still use LPM's normal install path.
- Interrupted dependency builds can resume their own checkpoints when resuming
  a parent. Deferred stage-2 jobs reuse matching checkpoints as well.
- An interrupted source/extraction phase is not considered safe to reuse;
  `--resume` refuses it and requires an explicit `--restart`.

A recipe may explicitly clean its own build tree. Buildpkg cannot undo such a
command. In particular, retrying an incomplete prepare phase reruns that phase.
Do not concurrently run bare make against a tree being managed by kmake.

## Kernel state

Kmake records the buildpkg session token at configuration time and refuses
objects belonging to another active buildpkg session. `kmake resume` aliases
incremental build without running configure or cleaning:

```sh
lpm kmake --source /tmp/src-linux/linux-7.2.11 --jobs 6 resume
```

That direct command only compiles. Use `lpm buildpkg ... --resume` to continue
through checks, package staging, packaging, installation and transaction hooks.

The old `KMAKE_RESUME=1` environment workaround alone is rejected before
workspace cleanup. Buildpkg owns phase selection; successful prepare is skipped
on resume without requiring any recipe-specific flag. Existing kernel recipes
that call kmake can use the new flag without adding another resume implementation.

## Workspace lifetime and migration

The default scratch paths remain `/tmp/src-NAME`, `/tmp/build-NAME`, and
`/tmp/pkg-NAME` for compatibility. State cannot recover data removed by reboot,
tmpfiles, an older LPM binary, or another program.

For long builds, configure this before the initial build, either in the
persistent LPM configuration or as an exported environment variable:

```sh
export LPM_BUILD_WORKDIR=/var/lib/lpm/work
```

The paths become `WORKDIR/NAME/{src,build,pkg}`. Keep the setting available when
resuming so saved paths can be checked against the configured workspace. Package
and compiler caches remain separate. Permission fallback directories are
recorded and validated rather than recomputed on resume.

Builds started by older LPM versions have no buildpkg checkpoint and cannot be
silently adopted. `--resume` fails without deleting them; existing untracked
kmake state also blocks automatic fresh cleanup. Preserve such trees and resume
Kbuild directly, or explicitly restart. Deleted objects cannot be recovered by
this feature.

## Validation

Integration tests exercise real source extraction/patching and shell phases,
failed build/check/stage/package retries, installation retry, dependency resume,
state/path tampering, missing directories, job changes, and kmake session
matching. A native C test kills a separate CLI/make process group after the first
object is compiled, then invokes `buildpkg --resume`, verifies that object's
mtime is unchanged, and produces a real package. Full kernel compilation and
hardware boot testing are not part of these tests.
