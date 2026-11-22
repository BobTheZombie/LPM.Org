"""Narrowly scoped privileged file modes for signed LPM packages.

Python's tarfile.data_filter strips setuid/setgid bits by design. LPM must
not weaken that filter or restore arbitrary privileged archive modes.
Only an explicitly allowlisted regular file from a signature-verified
package is eligible for restoration after the file content is validated.
"""
from __future__ import annotations

import re
import stat
from pathlib import Path
from typing import Any, Iterable, MutableMapping

# (package name, normalized manifest path): exact mode.
# fusermount3 is the setuid helper for unprivileged FUSE mounts.
PRIVILEGED_FILES = {
    ("fuse3", "/usr/bin/fusermount3"): 0o4755,
    ("dbus", "/usr/libexec/dbus-daemon-launch-helper"): 0o4750,
}
# Symbolic reference to the daemon account whose primary group can vary by
# distribution (for example "dbus" or "messagebus"). Never use host NSS.
PRIVILEGED_PRIMARY_USERS = {
    ("dbus", "/usr/libexec/dbus-daemon-launch-helper"): "dbus",
}
_PRIVILEGE_BITS = stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX
_SHA256 = re.compile(r"[0-9a-fA-F]{64}\Z")


def _safe_relative_path(raw_path: object) -> Path:
    if not isinstance(raw_path, str) or not raw_path.startswith("/"):
        raise ValueError("privileged manifest path must be absolute")
    parts = raw_path.split("/")[1:]
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise ValueError("privileged manifest path must be canonical")
    return Path(*parts)


def annotate_privileged_files(
    stagedir: Path, package_name: str, manifest: Iterable[MutableMapping[str, Any]]
) -> None:
    """Record setuid metadata only for the allowlisted staged binary.

    A missing setuid bit is a packaging error for an allowlisted file.
    The installer sets root ownership and resolves any named target group.
    The staging tree itself can be owned by an unprivileged builder.
    """
    stage = Path(stagedir)
    seen: set[str] = set()
    for entry in manifest:
        raw_path = entry.get("path")
        if not isinstance(raw_path, str):
            continue
        # Validate potentially privileged entries before opening a path.
        relative = _safe_relative_path(raw_path)
        file = stage / relative
        if not file.is_symlink() and file.is_file():
            mode = stat.S_IMODE(file.lstat().st_mode)
        else:
            mode = 0
        expected = PRIVILEGED_FILES.get((package_name, raw_path))
        if mode & _PRIVILEGE_BITS and mode != expected:
            raise ValueError(
                f"{package_name}: unsupported privileged file mode at {raw_path}"
            )
        if expected is None:
            continue
        if raw_path in seen:
            raise ValueError(f"duplicate privileged manifest entry: {raw_path}")
        seen.add(raw_path)
        if file.is_symlink() or not file.is_file() or mode != expected:
            raise ValueError(
                f"{package_name}: expected a regular {raw_path} with mode {expected:04o}"
            )
        if not _SHA256.fullmatch(str(entry.get("sha256", ""))):
            raise ValueError(f"{raw_path}: missing SHA-256 file digest")
        entry.update(mode=expected, uid=0, gid=PRIVILEGED_PRIMARY_USERS.get((package_name, raw_path), 0))

    # Don't silently produce a broken FUSE helper when it is present in the
    # manifest but cannot be staged with the required mode.
    for (name, path), expected in PRIVILEGED_FILES.items():
        if name == package_name and any(e.get("path") == path for e in manifest):
            if path not in seen:
                raise ValueError(f"{name}: missing privileged mode for {path}")



def resolve_privileged_gid(root: Path, package_name: str, raw_path: str, gid: object) -> int:
    """Resolve a privileged helper's primary group inside the target root.

    The manifest carries a symbolic, allowlisted service account name, never
    a build-host numeric GID. D-Bus uses its daemon user's primary group for
    access to dbus-daemon-launch-helper.
    """
    expected = PRIVILEGED_PRIMARY_USERS.get((package_name, raw_path))
    if expected is None:
        if type(gid) is not int or gid != 0:
            raise ValueError(f"{raw_path}: privileged group must be root")
        return 0
    if gid != expected:
        raise ValueError(f"{raw_path}: privileged owner must use {expected} primary group")

    passwd_file = Path(root) / "etc/passwd"
    group_file = Path(root) / "etc/group"
    try:
        users = passwd_file.read_text(encoding="utf-8").splitlines()
        groups = group_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(
            f"{raw_path}: cannot read target accounts {passwd_file} and {group_file}; "
            f"create the {expected} service account before installing"
        ) from exc

    user_ids: list[int] = []
    for line in users:
        parts = line.split(":")
        if len(parts) >= 7 and parts[0] == expected:
            if not parts[3].isascii() or not parts[3].isdecimal():
                raise ValueError(f"{raw_path}: invalid {expected} primary group ID")
            primary_gid = int(parts[3])
            if not 0 < primary_gid < 2**32:
                raise ValueError(f"{raw_path}: invalid {expected} primary group ID")
            user_ids.append(primary_gid)
    if len(user_ids) != 1:
        raise ValueError(
            f"{raw_path}: expected one {expected} service user in {passwd_file}; "
            "create the service account before installing"
        )

    primary_gid = user_ids[0]
    matching_groups = []
    for line in groups:
        parts = line.split(":")
        if len(parts) >= 4 and parts[2].isascii() and parts[2].isdecimal():
            if int(parts[2]) == primary_gid:
                matching_groups.append(parts)
    if len(matching_groups) != 1:
        raise ValueError(
            f"{raw_path}: expected one group with GID {primary_gid} in {group_file}; "
            "create the system group before installing"
        )

    # Match upstream's restriction on supplementary group memberships.
    members = [x for x in matching_groups[0][3].split(",") if x]
    if len(members) > 1:
        raise ValueError(f"{raw_path}: daemon's primary group has too many members")
    return primary_gid


def validate_privileged_files(
    package_name: str, manifest: Iterable[dict[str, Any]], *, verified: bool
) -> None:
    """Reject unsigned, spoofed or nonallowlisted setuid/setgid installs."""
    seen: set[str] = set()
    for entry in manifest:
        raw_path = entry.get("path")
        raw_mode = entry.get("mode")
        if raw_mode is None:
            # Do not allow signed or unsigned manifests to smuggle ownership
            # changes without an allowlisted privileged mode.
            if "uid" in entry or "gid" in entry:
                raise ValueError(f"{raw_path}: ownership metadata without privileged mode")
            continue
        if not isinstance(raw_mode, int) or isinstance(raw_mode, bool):
            raise ValueError(f"{raw_path}: invalid manifest mode")
        mode = stat.S_IMODE(raw_mode)
        if not mode & _PRIVILEGE_BITS:
            if "uid" in entry or "gid" in entry:
                raise ValueError(f"{raw_path}: ownership metadata without privileged mode")
            continue
        relative = _safe_relative_path(raw_path)
        del relative  # path validation only; no filesystem access here
        expected = PRIVILEGED_FILES.get((package_name, raw_path))
        if expected is None or mode != expected:
            raise ValueError(f"{package_name}: disallowed privileged mode for {raw_path}")
        if not verified:
            raise ValueError(f"{raw_path}: signed verification required for setuid install")
        if raw_path in seen:
            raise ValueError(f"{raw_path}: duplicated privileged file declaration")
        seen.add(raw_path)
        if entry.get("link") is not None:
            raise ValueError(f"{raw_path}: privileged symlink rejected")
        expected_gid = PRIVILEGED_PRIMARY_USERS.get((package_name, raw_path), 0)
        if entry.get("uid") != 0 or entry.get("gid") != expected_gid:
            raise ValueError(f"{raw_path}: privileged file must be root:{expected_gid}")
        if not _SHA256.fullmatch(str(entry.get("sha256", ""))):
            raise ValueError(f"{raw_path}: privileged file lacks SHA-256")
