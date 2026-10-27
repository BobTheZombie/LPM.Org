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

from .repo_categories import CategoryAmbiguityError, _gitlab_components, _choose

_LOCK = threading.RLock()
_REFRESH_SECONDS = 3600.0


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
        if len(parts) == 2 and parts[1] == parts[0] + ".lpmbuild":
            index.setdefault(parts[0], []).append(rel)
        elif len(parts) == 3 and parts[0].startswith("--") and parts[0].endswith("--") and parts[2] == parts[1] + ".lpmbuild":
            index.setdefault(parts[1], []).append(rel)
        elif len(parts) == 2 and parts[0].startswith("--") and parts[0].endswith("--"):
            name = parts[1][:-len(".lpmbuild")]
            index.setdefault(name, []).append(rel)
    return index


def cached_git_recipe(base_url: str, package: str, cache_dir: Path, *, refresh_seconds: float = _REFRESH_SECONDS) -> tuple[bytes, str] | None:
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
    root = Path(cache_dir) / key
    marker = root / ".lpm-last-recipe-sync"
    with _LOCK:
        fresh = root.is_dir() and (time.time() - marker.stat().st_mtime < refresh_seconds if marker.exists() else False)
        if not fresh:
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
                         "--branch", ref, _clone_url(scheme, host, project), str(staging / "checkout")],
                        check=True, capture_output=True, text=True, timeout=120,
                    )
                    if not root.exists():
                        os.replace(staging / "checkout", root)
                        marker.touch()
                except (OSError, subprocess.SubprocessError):
                    return None
                finally:
                    shutil.rmtree(staging, ignore_errors=True)

        if not root.is_dir():
            return None
        path = _choose(package, _recipe_index(root, prefix))
        if path is None:
            return None
        file_path = root / prefix / path if prefix else root / path
        return file_path.read_bytes(), path
