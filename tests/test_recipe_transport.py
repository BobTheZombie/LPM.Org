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


def test_operator_managed_checkout_without_network(tmp_path, monkeypatch):
    local = tmp_path / "packages"
    recipe = local / "foo" / "foo.lpmbuild"
    recipe.parent.mkdir(parents=True)
    recipe.write_bytes(b"NAME=foo\nVERSION=1\n")
    monkeypatch.setattr(snapshot, "_clone_url", lambda *args: (_ for _ in ()).throw(
        AssertionError("unexpected Git network operation")))
    assert snapshot.cached_git_recipe(BASE, "foo", tmp_path / "cache", local_checkout=local) == (
        b"NAME=foo\nVERSION=1\n", "foo/foo.lpmbuild"
    )


def test_offline_mode_uses_existing_snapshot(tmp_path, monkeypatch):
    remote = tmp_path / "remote"
    remote.mkdir()
    _git("init", "-b", "main", remote)
    _git("-C", remote, "config", "user.name", "LPM tests")
    _git("-C", remote, "config", "user.email", "lpm-tests@example.invalid")
    recipe = remote / "foo" / "foo.lpmbuild"
    recipe.parent.mkdir()
    recipe.write_bytes(b"NAME=foo\nVERSION=1\n")
    _git("-C", remote, "add", ".")
    _git("-C", remote, "commit", "-m", "first")
    monkeypatch.setattr(snapshot, "_clone_url", lambda *args: str(remote))
    cache = tmp_path / "cache"
    first = snapshot.cached_git_recipe(BASE, "foo", cache)
    assert first is not None
    assert snapshot.cached_git_recipe(BASE, "foo", cache, offline=True, refresh_seconds=0) == first
    assert snapshot.cached_git_recipe(BASE, "foo", tmp_path / "empty", offline=True) is None


def test_git_only_transport_never_falls_back_to_gitlab_http(tmp_path, monkeypatch):
    from lpm import app
    monkeypatch.setitem(app.CONF, "LPMBUILD_REPO", BASE)
    monkeypatch.setitem(app.CONF, "LPMBUILD_RECIPE_TRANSPORT", "git")
    monkeypatch.setitem(app.CONF, "LPMBUILD_RECIPE_LOCAL_PATH", str(tmp_path / "missing"))
    monkeypatch.setattr(app, "cached_git_recipe", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "rate_limited_read", lambda *args: (_ for _ in ()).throw(
        AssertionError("Git transport called GitLab HTTP")))
    with pytest.raises(SystemExit) as exc:
        app.fetch_lpmbuild("missing", tmp_path / "missing.lpmbuild")
    assert exc.value.code == 2


def test_hybrid_mode_explicitly_falls_back_to_gitlab_http(tmp_path, monkeypatch):
    from lpm import app, repo_categories
    repo_categories._TREE_CACHE.clear()
    repo_categories._REFRESHED_BASES.clear()
    monkeypatch.setitem(app.CONF, "LPMBUILD_REPO", BASE)
    monkeypatch.setitem(app.CONF, "LPMBUILD_RECIPE_TRANSPORT", "hybrid")
    monkeypatch.setitem(app.CONF, "LPMBUILD_RECIPE_LOCAL_PATH", str(tmp_path / "missing"))
    monkeypatch.setattr(app, "cached_git_recipe", lambda *args, **kwargs: None)
    calls = []
    def fake_reader(reader, url):
        calls.append(url)
        if "/repository/tree?" in url:
            return b'[{"type":"blob","path":"foo/foo.lpmbuild"}]', url
        return b"NAME=foo\nVERSION=1\n", url
    monkeypatch.setattr(app, "rate_limited_read", fake_reader)
    dest = tmp_path / "foo.lpmbuild"
    assert app.fetch_lpmbuild("foo", dest) == dest
    assert dest.read_bytes() == b"NAME=foo\nVERSION=1\n"
    assert len(calls) == 2
