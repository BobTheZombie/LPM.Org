"""Git snapshot recipe transport and GitLab 429 request coalescing."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

ROOT = Path(__file__).resolve().parents[1] / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lpm import repo_git_snapshot as snapshot
from lpm import repo_rate_limit as limiter

BASE = "https://gitlab.com/lpm-org/packages/-/raw/main"


@pytest.fixture(autouse=True)
def reset_transport_state():
    snapshot._FAILED_UNTIL.clear()
    snapshot._INDEX_CACHE.clear()
    limiter._BLOCKED_UNTIL.clear()
    yield
    snapshot._FAILED_UNTIL.clear()
    snapshot._INDEX_CACHE.clear()
    limiter._BLOCKED_UNTIL.clear()


def _git(*args):
    subprocess.run(["git", *map(str, args)], check=True, capture_output=True)


def test_git_snapshot_reuses_checkout_and_updates_after_ttl(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    _git("init", "-b", "main", remote)
    _git("-C", remote, "config", "user.name", "LPM tests")
    _git("-C", remote, "config", "user.email", "lpm-tests@example.invalid")
    path = remote / "--KDE--" / "kdoctools6" / "kdoctools6.lpmbuild"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"NAME=kdoctools6\nVERSION=1\n")
    _git("-C", remote, "add", ".")
    _git("-C", remote, "commit", "-m", "first")
    monkeypatch.setattr(snapshot, "_clone_url", lambda *args: str(remote))

    cache = tmp_path / "cache"
    first = snapshot.cached_git_recipe(BASE, "kdoctools6", cache)
    assert first == (b"NAME=kdoctools6\nVERSION=1\n", "--KDE--/kdoctools6/kdoctools6.lpmbuild")

    path.write_bytes(b"NAME=kdoctools6\nVERSION=2\n")
    _git("-C", remote, "add", ".")
    _git("-C", remote, "commit", "-m", "second")

    assert snapshot.cached_git_recipe(BASE, "kdoctools6", cache) == first
    # Expire a snapshot without sleeping.
    checkout = next(cache.iterdir())
    marker = checkout / ".lpm-last-recipe-sync"
    marker.touch()
    import os
    os.utime(marker, (1, 1))
    assert snapshot.cached_git_recipe(BASE, "kdoctools6", cache, refresh_seconds=60) == (
        b"NAME=kdoctools6\nVERSION=2\n",
        "--KDE--/kdoctools6/kdoctools6.lpmbuild",
    )


def test_git_snapshot_preserves_flat_priority(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    _git("init", "-b", "main", remote)
    _git("-C", remote, "config", "user.name", "LPM tests")
    _git("-C", remote, "config", "user.email", "lpm-tests@example.invalid")
    for name, relative in [
        ("flat", "glibc/glibc.lpmbuild"),
        ("other", "--KDE--/glibc/glibc.lpmbuild"),
    ]:
        f = remote / relative
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(name.encode())
    _git("-C", remote, "add", ".")
    _git("-C", remote, "commit", "-m", "recipes")
    monkeypatch.setattr(snapshot, "_clone_url", lambda *args: str(remote))
    assert snapshot.cached_git_recipe(BASE, "glibc", tmp_path / "cache") == (
        b"flat", "glibc/glibc.lpmbuild"
    )


def test_429_halts_repeated_requests_and_preserves_retry_after():
    calls = []

    def reader(url):
        calls.append(url)
        reason = HTTPError(url, 429, "Too Many Requests", {"Retry-After": "3202"}, None)
        raise RuntimeError("Failed to read URL") from reason

    with pytest.raises(limiter.RecipeRateLimited, match="Retry-After=3202s"):
        limiter.rate_limited_read(reader, BASE + "/--KDE--/kwin/kwin.lpmbuild")
    with pytest.raises(limiter.RecipeRateLimited, match="retry in"):
        limiter.rate_limited_read(reader, BASE + "/--KDE--/plasma-desktop/plasma-desktop.lpmbuild")
    assert len(calls) == 1


def test_index_does_not_wrap_429_as_missing_recipe():
    from lpm.repo_categories import discover_category_path, _TREE_CACHE, _REFRESHED_BASES
    _TREE_CACHE.clear()
    _REFRESHED_BASES.clear()

    def reader(url):
        raise HTTPError(url, 429, "Too Many Requests", {"Retry-After": "100"}, None)

    with pytest.raises(limiter.RecipeRateLimited):
        discover_category_path(BASE, "kdoctools6", lambda url: limiter.rate_limited_read(reader, url))
