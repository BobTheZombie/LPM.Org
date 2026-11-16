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
    Ownership is recorded as root:root for the final installation; the
    staging tree itself can be owned by an unprivileged builder.
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
        entry.update(mode=expected, uid=0, gid=0)

    # Don't silently produce a broken FUSE helper when it is present in the
    # manifest but cannot be staged with the required mode.
    for (name, path), expected in PRIVILEGED_FILES.items():
        if name == package_name and any(e.get("path") == path for e in manifest):
            if path not in seen:
                raise ValueError(f"{name}: missing privileged mode for {path}")


def validate_privileged_files(
    package_name: str, manifest: Iterable[dict[str, Any]], *, verified: bool
) -> None:
    """Reject unsigned, spoofed or nonallowlisted setuid/setgid installs."""
    seen: set[str] = set()
    for entry in manifest:
        raw_path = entry.get("path")
        raw_mode = entry.get("mode")
        if raw_mode is None:
            # An allowlisted file that lost the declaration must not be
            # silently installed without the expected security metadata.
            continue
        if not isinstance(raw_mode, int) or isinstance(raw_mode, bool):
            raise ValueError(f"{raw_path}: invalid manifest mode")
        mode = stat.S_IMODE(raw_mode)
        if not mode & _PRIVILEGE_BITS:
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
        if entry.get("uid") != 0 or entry.get("gid") != 0:
            raise ValueError(f"{raw_path}: privileged file must be root:root")
        if not _SHA256.fullmatch(str(entry.get("sha256", ""))):
            raise ValueError(f"{raw_path}: privileged file lacks SHA-256")
