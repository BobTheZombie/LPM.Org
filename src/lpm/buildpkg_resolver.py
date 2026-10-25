"""Resolve LPM buildpkg inputs as local recipes or remote package names.

Local recipes have priority. Remote lookup only applies to a simple package
name or its optional .lpmbuild suffix; missing explicit paths never fall back
silently to a different remote recipe.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Callable

from .repo_categories import valid_package_name

_CATEGORY = re.compile(r"^--[A-Za-z0-9][A-Za-z0-9_-]*--$")


class BuildpkgRecipeResolutionError(ValueError):
    """Unable to find a usable build recipe for the requested CLI input."""


def _local_recipe_candidates(name: str, root: Path):
    """Find local package trees, including source categories, without recursion."""
    filename = f"{name}.lpmbuild"
    yield root / filename
    yield root / name / filename
    for base in (root / "packages", root):
        yield base / name / filename
        if not base.is_dir():
            continue
        for category in sorted(base.iterdir()):
            if category.is_dir() and _CATEGORY.fullmatch(category.name):
                yield category / name / filename
                yield category / filename


def resolve_buildpkg_recipe(
    spec: str | Path,
    fetch: Callable[[str, Path], Path],
    *,
    root: Path | None = None,
    download_dir: Path | None = None,
) -> tuple[Path, str]:
    """Return (recipe path, 'local'|'remote') for an LPM buildpkg request.

    - An existing local .lpmbuild path always wins.
    - Bare package names and bare .lpmbuild filenames search local trees,
      then fetch from the configured remote source repository.
    - Explicit paths never download an unrelated remote recipe if missing.
    """
    value = str(spec)
    if not value or not value.strip():
        raise BuildpkgRecipeResolutionError("package name or .lpmbuild path is required")
    path = Path(value).expanduser()
    if path.is_file():
        if path.suffix != ".lpmbuild":
            raise BuildpkgRecipeResolutionError(f"not a .lpmbuild recipe: {path}")
        return path, "local"

    # argparse must preserve the original string: Path('./foo') erases './'.
    explicit_path = (
        path.is_absolute() or value.startswith((".", "~")) or
        "/" in value or "\\" in value
    )
    # A bare name can itself be an existing local package directory.
    # Continue searching for <name>/<name>.lpmbuild in that case.
    if path.exists() and (not path.is_dir() or explicit_path or value.endswith(".lpmbuild")):
        raise BuildpkgRecipeResolutionError(f"not a regular .lpmbuild file: {path}")

    if explicit_path:
        raise BuildpkgRecipeResolutionError(
            f"local .lpmbuild path not found: {value}. "
            "To fetch from the source repository, pass only the package name."
        )

    name = value.removesuffix(".lpmbuild")
    if not valid_package_name(name):
        raise BuildpkgRecipeResolutionError(f"invalid package name: {name!r}")

    local_root = Path.cwd() if root is None else Path(root)
    for candidate in _local_recipe_candidates(name, local_root):
        if candidate.is_file():
            return candidate, "local"

    target_dir = Path(tempfile.gettempdir()) if download_dir is None else Path(download_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"lpm-{name}.lpmbuild"
    try:
        fetched = Path(fetch(name, target))
    except Exception as exc:
        raise BuildpkgRecipeResolutionError(
            f"cannot resolve {name!r} locally or in remote lpmbuild repository: {exc}"
        ) from exc
    if not fetched.is_file():
        raise BuildpkgRecipeResolutionError(
            f"remote repository returned no .lpmbuild recipe for {name!r}"
        )
    return fetched, "remote"
