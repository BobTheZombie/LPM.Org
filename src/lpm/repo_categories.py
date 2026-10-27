"""Index flat and categorized LPM recipes from the GitLab repository tree.

A category marker such as --KDE-- is a source layout hint, not a package name.
"""
from __future__ import annotations

import json
import re
import time
from .repo_rate_limit import RecipeRateLimited
from urllib.parse import quote, urlencode, urlsplit

_CATEGORY = re.compile(r"^--[A-Za-z0-9][A-Za-z0-9_-]*--$")
_PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_PAGE_SIZE = 100
_MAX_PAGES = 250
_CACHE_TTL = 86400.0  # Reuse index for long-running source builds.
# base URL -> (expiry, {package name: [repository-relative recipe paths]})
_TREE_CACHE: dict[str, tuple[float, dict[str, list[str]]]] = {}
# Prevent one forced API refresh for every unavailable dependency.
_REFRESHED_BASES: set[str] = set()


class CategoryLookupError(RuntimeError):
    """GitLab recipe index lookup failed."""


class CategoryAmbiguityError(CategoryLookupError):
    """Multiple categorized recipes exist without a flat override."""


def valid_package_name(name: str) -> bool:
    """Do not interpolate slashes or traversal into raw-file URLs."""
    return bool(_PACKAGE.fullmatch(name)) and name not in {".", ".."}


def _gitlab_components(base_url: str):
    """Parse configured GitLab /-/raw/<ref>[/optional/root] URLs."""
    parsed = urlsplit(base_url.rstrip("/"))
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        return None
    project_path, marker, tail = parsed.path.partition("/-/raw/")
    if not marker or not project_path.strip("/") or not tail:
        return None
    ref, _, prefix = tail.partition("/")
    if not ref:
        return None
    return parsed.scheme, parsed.netloc, project_path.strip("/"), ref, prefix.strip("/")


def supports_category_discovery(base_url: str) -> bool:
    return _gitlab_components(base_url) is not None


def _choose(package: str, index: dict[str, list[str]]) -> str | None:
    paths = sorted(set(index.get(package, [])))
    # Prefer the historical flat path without probing a missing URL first.
    flat = f"{package}/{package}.lpmbuild"
    if flat in paths:
        return flat
    if len(paths) > 1:
        raise CategoryAmbiguityError(
            f"package {package!r} occurs in multiple categories: "
            + ", ".join(paths)
            + "; keep one recipe or move the desired recipe to the repository root"
        )
    return paths[0] if paths else None


def cached_category_path(base_url: str, package: str) -> str | None:
    """Reuse a previously loaded index (including during recursive builds)."""
    cached = _TREE_CACHE.get(base_url.rstrip("/"))
    if cached is None or cached[0] <= time.monotonic():
        return None
    return _choose(package, cached[1])


def discover_category_path(base_url: str, package: str, reader, *, refresh: bool = False) -> str | None:
    """Find flat or --CATEGORY-- recipes using one cached GitLab tree.

    `reader` is LPM's existing urlread callable returning (bytes, final_url).
    Fetch all pages before caching, to avoid partially populated indexes.
    """
    if not valid_package_name(package):
        raise CategoryLookupError(f"invalid package name: {package!r}")
    base_url = base_url.rstrip("/")
    components = _gitlab_components(base_url)
    if components is None:
        return None

    cached = _TREE_CACHE.get(base_url)
    if cached is not None and cached[0] > time.monotonic():
        if not refresh or base_url in _REFRESHED_BASES:
            return _choose(package, cached[1])
    if refresh:
        _REFRESHED_BASES.add(base_url)

    scheme, host, project, ref, prefix = components
    endpoint = (
        f"{scheme}://{host}/api/v4/projects/"
        f"{quote(project, safe='')}/repository/tree"
    )
    index: dict[str, list[str]] = {}
    for page in range(1, _MAX_PAGES + 1):
        params = {"ref": ref, "recursive": "true", "per_page": _PAGE_SIZE, "page": page}
        if prefix:
            params["path"] = prefix
        url = f"{endpoint}?{urlencode(params)}"
        try:
            payload, _ = reader(url)
            records = json.loads(payload)
        except RecipeRateLimited:
            raise
        except Exception as exc:
            raise CategoryLookupError(
                f"could not enumerate categories in {base_url}: {exc}"
            ) from exc
        if not isinstance(records, list):
            raise CategoryLookupError(f"invalid GitLab repository tree response: {url}")

        for record in records:
            if not isinstance(record, dict) or record.get("type") != "blob":
                continue
            path = record.get("path")
            if not isinstance(path, str):
                continue
            if prefix:
                if not path.startswith(prefix + "/"):
                    continue
                path = path[len(prefix) + 1:]
            parts = path.split("/")
            if len(parts) == 3 and _CATEGORY.fullmatch(parts[0]):
                _, name, filename = parts
                if valid_package_name(name) and filename == name + ".lpmbuild":
                    index.setdefault(name, []).append(path)
            elif len(parts) == 2:
                folder, filename = parts
                if _CATEGORY.fullmatch(folder):
                    if filename.endswith(".lpmbuild"):
                        name = filename[:-len(".lpmbuild")]
                        if valid_package_name(name):
                            index.setdefault(name, []).append(path)
                elif valid_package_name(folder) and filename == folder + ".lpmbuild":
                    index.setdefault(folder, []).append(path)

        if len(records) < _PAGE_SIZE:
            break
    else:
        raise CategoryLookupError(
            f"GitLab repository tree exceeds {_PAGE_SIZE * _MAX_PAGES} entries"
        )

    if len(_TREE_CACHE) >= 16:
        _TREE_CACHE.clear()
    _TREE_CACHE[base_url] = (time.monotonic() + _CACHE_TTL, index)
    return _choose(package, index)
