"""Package-name and local-file resolution behavior for lpm buildpkg."""
from pathlib import Path

import pytest

from lpm.buildpkg_resolver import (
    BuildpkgRecipeResolutionError,
    resolve_buildpkg_recipe,
)


def _unexpected_fetch(name, dst):
    raise AssertionError(f"unexpected remote lookup: {name}")


def test_explicit_local_file_is_used(tmp_path):
    file = tmp_path / "custom.lpmbuild"
    file.write_text("NAME=local\n")
    assert resolve_buildpkg_recipe(str(file), _unexpected_fetch) == (file, "local")


def test_bare_local_recipe_has_priority(tmp_path):
    recipe = tmp_path / "kwin.lpmbuild"
    recipe.write_text("NAME=kwin\n")
    assert resolve_buildpkg_recipe("kwin", _unexpected_fetch, root=tmp_path) == (recipe, "local")
    assert resolve_buildpkg_recipe("kwin.lpmbuild", _unexpected_fetch, root=tmp_path) == (recipe, "local")


def test_existing_package_directory_resolves_recipe(tmp_path):
    recipe = tmp_path / "qt6-base/qt6-base.lpmbuild"
    recipe.parent.mkdir()
    recipe.write_text("NAME=qt6-base\n")
    assert resolve_buildpkg_recipe("qt6-base", _unexpected_fetch, root=tmp_path) == (recipe, "local")


def test_local_flat_and_categorized_trees(tmp_path):
    flat = tmp_path / "packages/qt6-base/qt6-base.lpmbuild"
    flat.parent.mkdir(parents=True)
    flat.write_text("NAME=qt6-base\n")
    kde = tmp_path / "--KDE--/kwin/kwin.lpmbuild"
    kde.parent.mkdir(parents=True)
    kde.write_text("NAME=kwin\n")
    category_flat = tmp_path / "packages/--KDE--/kio.lpmbuild"
    category_flat.parent.mkdir(parents=True)
    category_flat.write_text("NAME=kio\n")
    assert resolve_buildpkg_recipe("qt6-base", _unexpected_fetch, root=tmp_path) == (flat, "local")
    assert resolve_buildpkg_recipe("kwin", _unexpected_fetch, root=tmp_path) == (kde, "local")
    assert resolve_buildpkg_recipe("kio", _unexpected_fetch, root=tmp_path) == (category_flat, "local")


def test_bare_name_fetches_remote_when_missing(tmp_path):
    calls = []

    def fetch(name, dst):
        calls.append((name, dst))
        dst.write_text("NAME=plasma6-full\n")
        return dst

    path, origin = resolve_buildpkg_recipe(
        "plasma6-full", fetch, root=tmp_path, download_dir=tmp_path / "downloads"
    )
    assert origin == "remote"
    assert path.name == "lpm-plasma6-full.lpmbuild"
    assert calls == [("plasma6-full", path)]
    assert path.read_text() == "NAME=plasma6-full\n"


def test_suffix_fetches_remote_without_using_suffix_as_package_name(tmp_path):
    names = []

    def fetch(name, dst):
        names.append(name)
        dst.write_text("NAME=foo\n")
        return dst

    resolve_buildpkg_recipe("foo.lpmbuild", fetch, root=tmp_path, download_dir=tmp_path)
    assert names == ["foo"]


@pytest.mark.parametrize("input", [
    "./missing.lpmbuild",
    "/path/to/missing.lpmbuild",
    "--KDE--/kwin/kwin.lpmbuild",
    "packages/kwin/kwin.lpmbuild",
])
def test_missing_explicit_paths_are_not_remote_fetched(tmp_path, input):
    with pytest.raises(BuildpkgRecipeResolutionError, match="local .lpmbuild path not found"):
        resolve_buildpkg_recipe(input, _unexpected_fetch, root=tmp_path)


def test_remote_failure_is_informative(tmp_path):
    def failed(name, dst):
        raise OSError("GitLab HTTP 404")

    with pytest.raises(BuildpkgRecipeResolutionError, match="GitLab HTTP 404"):
        resolve_buildpkg_recipe("missing", failed, root=tmp_path)


def test_remote_fetch_must_produce_a_file(tmp_path):
    with pytest.raises(BuildpkgRecipeResolutionError, match="returned no"):
        resolve_buildpkg_recipe("missing", lambda name, dst: dst, root=tmp_path, download_dir=tmp_path)


@pytest.mark.parametrize("value", ["../escape", "", "  ", "bad/name"])
def test_malformed_names_are_rejected(tmp_path, value):
    with pytest.raises(BuildpkgRecipeResolutionError):
        resolve_buildpkg_recipe(value, _unexpected_fetch, root=tmp_path)


def test_existing_directory_is_not_recipe(tmp_path):
    d = tmp_path / "foo.lpmbuild"
    d.mkdir()
    with pytest.raises(BuildpkgRecipeResolutionError, match="not a regular"):
        resolve_buildpkg_recipe(str(d), _unexpected_fetch)
