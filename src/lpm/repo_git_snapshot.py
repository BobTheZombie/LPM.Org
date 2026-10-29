"""Persistent Git checkout for GitLab-hosted .lpmbuild recipes.

One shallow clone replaces hundreds of raw requests during large SAT builds.
The snapshot is a managed cache; SOURCE paths still use their original remote URL.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .repo_categories import _gitlab_components, _choose, _CATEGORY, valid_package_name

_LOCK = threading.RLock()
_REFRESH_SECONDS = 3600.0
_FAILED_UNTIL: dict[str, float] = {}
_INDEX_CACHE: dict[str, tuple[float, dict[str, list[str]]]] = {}


def _clone_url(scheme: str, host: str, project: str) -> str:
    return f"{scheme}://{host}/{project}.git"


def _recipe_index(checkout: Path, prefix: str) -> dict[str, list[str]]:
    """Index recipe paths relative to the configured raw URL root."""
    root = checkout / prefix if prefix else checkout
    index: dict[str, list[str]] = {}
    if not root.is_dir():
        return index
    for path in root.rglob("*.lpmbuild"):
        rel = path.relative_to(root).as_posix()
        parts = rel.split("/")
        if len(parts) == 2 and valid_package_name(parts[0]) and parts[1] == parts[0] + ".lpmbuild":
            index.setdefault(parts[0], []).append(rel)
        elif len(parts) == 3 and _CATEGORY.fullmatch(parts[0]) and valid_package_name(parts[1]) and parts[2] == parts[1] + ".lpmbuild":
            index.setdefault(parts[1], []).append(rel)
        elif len(parts) == 2 and _CATEGORY.fullmatch(parts[0]):
            name = parts[1][:-len(".lpmbuild")]
            if valid_package_name(name):
                index.setdefault(name, []).append(rel)
    return index


def cached_git_recipe(
    base_url: str, package: str, cache_dir: Path, *,
    refresh_seconds: float = _REFRESH_SECONDS,
    local_checkout: Path | None = None, offline: bool = False,
    clone_url: str | None = None,
) -> tuple[bytes, str] | None:
    """Return (payload, path relative to base_url), or None if not available.

    The cache is updated at most once per refresh interval per process, and
    failed refreshes retain an existing checkout for offline use.
    """
    components = _gitlab_components(base_url)
    if components is None:
        return None
    scheme, host, project, ref, prefix = components
    if scheme != "https" or host.lower() != "gitlab.com":
        return None
    if not project or any(part in {"", ".", ".."} for part in project.split("/")):
        return None

    key = hashlib.sha256(base_url.rstrip("/").encode()).hexdigest()[:24]
    # An operator-managed checkout is read-only from LPM's perspective.
    local = Path(local_checkout) if local_checkout is not None else None
    manual = local is not None and local.is_dir()
    root = local if manual else Path(cache_dir) / key
    marker = root / ".lpm-last-recipe-sync"
    with _LOCK:
        if not manual and time.monotonic() < _FAILED_UNTIL.get(key, 0) and not root.is_dir():
            return None
        fresh = root.is_dir() and (time.time() - marker.stat().st_mtime < refresh_seconds if marker.exists() else False)
        if not (manual or offline or fresh):
            root.parent.mkdir(parents=True, exist_ok=True)
            if (root / ".git").is_dir():
                try:
                    subprocess.run(
                        ["git", "-C", str(root), "fetch", "--depth=1", "origin", ref],
                        check=True, capture_output=True, text=True, timeout=120,
                    )
                    subprocess.run(
                        ["git", "-C", str(root), "reset", "--hard", "FETCH_HEAD"],
                        check=True, capture_output=True, text=True, timeout=60,
                    )
                    marker.touch()
                except (OSError, subprocess.SubprocessError):
                    # Avoid hammering the remote when disconnected; stale
                    # snapshots remain useful until the next refresh interval.
                    marker.touch()
            else:
                staging = Path(tempfile.mkdtemp(prefix=".lpm-recipes-", dir=root.parent))
                try:
                    subprocess.run(
                        ["git", "clone", "--quiet", "--depth=1", "--single-branch",
                         "--branch", ref, clone_url or _clone_url(scheme, host, project), str(staging / "checkout")],
                        check=True, capture_output=True, text=True, timeout=120,
                    )
                    if not root.exists():
                        os.replace(staging / "checkout", root)
                        marker.touch()
                except (OSError, subprocess.SubprocessError):
                    _FAILED_UNTIL[key] = time.monotonic() + min(refresh_seconds, 3600)
                    return None
                finally:
                    shutil.rmtree(staging, ignore_errors=True)

        if not root.is_dir():
            return None
        cache_key = str(root / prefix)
        if manual:
            # Invalidate cached inventory after an operator runs git pull.
            head_file = root / ".git" / "HEAD"
            try:
                head = head_file.read_text(encoding="utf-8").strip()
                ref_file = root / ".git" / head[5:].strip() if head.startswith("ref: ") else head_file
                generation = ref_file.stat().st_mtime_ns
            except OSError:
                generation = root.stat().st_mtime_ns
        else:
            generation = marker.stat().st_mtime_ns if marker.exists() else 0
        cached = _INDEX_CACHE.get(cache_key)
        if cached is None or cached[0] != generation:
            index = _recipe_index(root, prefix)
            _INDEX_CACHE[cache_key] = (generation, index)
        else:
            index = cached[1]
        path = _choose(package, index)
        if path is None:
            return None
        file_path = root / prefix / path if prefix else root / path
        # Reject symlinks or paths outside the checked-out repository.
        if (file_path.is_symlink() or not file_path.is_file()
                or not file_path.resolve().is_relative_to(root.resolve())):
            return None
        return file_path.read_bytes(), path
