# Signed privileged file mode policy

LPM extracts package archives with Python's `tarfile` `data` filter. That
filter intentionally strips the setuid, setgid and sticky bits from archived
files. **Do not disable it**, and do not apply arbitrary tar header modes on
the live root filesystem.

A small allowlist in `src/lpm/privileged_files.py` permits restoring the
setuid mode only after validating the embedded package manifest and cryptographic
package signature. At present the only privileged payload is:

| Package | Path | Required mode | Required installed owner |
|---|---|---|---|
| fuse3 | /usr/bin/fusermount3 | 4755 | 0:0 |

Building an LPM package with that specific staged file at mode `4755`
produces manifest entries for `mode`, `uid`, `gid` and `sha256`.
The archive is signed by LPM's existing signing mechanism; the installer
refuses to apply the privileged mode when signature verification is disabled,
even with `--no-verify`.

During install LPM:

1. Verifies the signature using configured trusted public keys.
2. Rejects malformed or unallowlisted privilege declarations.
3. Extracts with `filter="data"` and validates hashes.
4. Places the file and changes ownership to `root:root`.
5. Only then restores the exact `4755` mode.
6. Re-verifies the final mode, uid/gid, digest and size before committing.

No arbitrary `chmod` command is added to install hooks or to a generated
maintainer script; privileges are not granted from an unsigned artifact.

The FUSE3 recipe stages `fusermount3` as mode `4755` while running Meson
with `-Duseroot=false`. This avoids creating `/dev/fuse` in a staging
directory or requiring a root-owned builder. The installed helper receives
root ownership only at the trusted LPM install step.

## Operator verification

```sh
lpm buildpkg fuse3
stat -c '%a %u:%g %n' /usr/bin/fusermount3
findmnt -T /usr/bin/fusermount3 -o TARGET,OPTIONS
ls -l /dev/fuse
/usr/libexec/lpm/flatpak-fuse-check
```

The expected installed mode is `4755`, owner `0:0`. A `nosuid` mount
option on the helper's filesystem can still defeat setuid behavior.
`/dev/fuse` must exist and be accessible to the calling user.

**Important:** Privileged file modes in other packages require a deliberate,
reviewed policy entry. The rule is not widened based on archive contents.
Read-only diagnostics report problems; do not blindly chmod a live filesystem.
