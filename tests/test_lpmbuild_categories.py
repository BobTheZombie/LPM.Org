"""GitLab --CATEGORY-- package tree discovery and remote recipe fetching."""
import json
import sys
import types
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from lpm.repo_categories import (
    CategoryLookupError,
    _TREE_CACHE,
    discover_category_path,
)


BASE = "https://gitlab.com/lpm-org/packages/-/raw/main"


@pytest.fixture(autouse=True)
def clear_category_cache():
    _TREE_CACHE.clear()
    yield
    _TREE_CACHE.clear()


def _blob(path):
    return {"type": "blob", "path": path, "name": path.split("/")[-1]}


def test_gitlab_category_lookup_and_index_reuse():
    urls = []
    entries = [
        {"type": "tree", "path": "--KDE--"},
        _blob("--KDE--/qt6-base/qt6-base.lpmbuild"),
        _blob("--KDE--/plasma-meta/plasma-meta.lpmbuild"),
        _blob("--KDE--/qt6-svg.lpmbuild"),
        _blob("glibc/glibc.lpmbuild"),
    ]

    def reader(url):
        urls.append(url)
        return json.dumps(entries).encode(), url

    assert discover_category_path(BASE, "qt6-base", reader) == (
        "--KDE--/qt6-base/qt6-base.lpmbuild"
    )
    assert discover_category_path(BASE, "qt6-svg", reader) == "--KDE--/qt6-svg.lpmbuild"
    assert discover_category_path(BASE, "glibc", reader) is None
    assert len(urls) == 1
    assert "/api/v4/projects/lpm-org%2Fpackages/repository/tree?" in urls[0]
    assert "ref=main" in urls[0]


def test_gitlab_category_pagination_and_subdirectory():
    base = BASE + "/recipes"
    first_page = [_blob("recipes/unrelated/file.txt") for _ in range(100)]
    second_page = [_blob("recipes/--KDE--/kwin/kwin.lpmbuild")]
    calls = []

    def reader(url):
        calls.append(url)
        from urllib.parse import parse_qs, urlsplit
        page = parse_qs(urlsplit(url).query)["page"][0]
        return json.dumps(first_page if page == "1" else second_page).encode(), url

    assert discover_category_path(base, "kwin", reader) == "--KDE--/kwin/kwin.lpmbuild"
    assert len(calls) == 2
    assert "path=recipes" in calls[0]


def test_gitlab_category_duplicate_detection():
    entries = [
        _blob("--KDE--/kwin/kwin.lpmbuild"),
        _blob("--EXTRA--/kwin/kwin.lpmbuild"),
    ]

    def reader(url):
        return json.dumps(entries).encode(), url

    with pytest.raises(CategoryLookupError, match="multiple categories"):
        discover_category_path(BASE, "kwin", reader)


def test_invalid_package_name_is_rejected():
    def fail(url):
        raise AssertionError(f"unexpected network call: {url}")

    with pytest.raises(CategoryLookupError, match="invalid package name"):
        discover_category_path(BASE, "../kwin", fail)


def _import_lpm(tmp_path, monkeypatch):
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    if "tqdm" not in sys.modules:
        module = types.ModuleType("tqdm")

        class Dummy:
            def __init__(self, iterable=None, **kwargs):
                self.iterable = iterable or []
                self.n = 0
                self.total = kwargs.get("total", 0)

            def __iter__(self):
                return iter(self.iterable)

            def update(self, *args, **kwargs):
                pass

            def set_description(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        module.tqdm = Dummy
        monkeypatch.setitem(sys.modules, "tqdm", module)
    import importlib

    return importlib.import_module("lpm")


def test_remote_fetch_category_relative_source_origin(tmp_path, monkeypatch):
    lpm = _import_lpm(tmp_path, monkeypatch)
    monkeypatch.setitem(lpm.CONF, "LPMBUILD_REPO", BASE)
    urls = []
    recipe = b'NAME="qt6-base"\nVERSION="6.11.2"\nSOURCE=("patch.diff")\n'
    tree = [_blob("--KDE--/qt6-base/qt6-base.lpmbuild")]

    def reader(url, timeout=10):
        urls.append(url)
        if "/api/v4/projects/" in url:
            return json.dumps(tree).encode(), url
        if url == BASE + "/--KDE--/qt6-base/qt6-base.lpmbuild":
            return recipe, url
        if url == BASE + "/--KDE--/qt6-base/patch.diff":
            return b"patch-data", url
        raise FileNotFoundError(url)

    monkeypatch.setattr(lpm, "urlread", reader)
    dst = tmp_path / "qt6-base.lpmbuild"
    assert lpm.fetch_lpmbuild("qt6-base", dst) == dst
    assert dst.read_bytes() == recipe
    from lpm import app
    assert app._LPMBUILD_RECIPE_SOURCE_BASES[str(dst.resolve())] == (
        BASE + "/--KDE--/qt6-base/"
    )
    assert urls[0] == BASE + "/qt6-base/qt6-base.lpmbuild"
    assert urls[2] == BASE + "/--KDE--/qt6-base/qt6-base.lpmbuild"

    # The downloaded recipe's relative sources come from its remote category,
    # even though the script itself was downloaded to a temporary directory.
    monkeypatch.setattr(lpm, "sandboxed_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(lpm, "generate_install_script", lambda stagedir: "true")
    monkeypatch.setattr(
        lpm, "build_package",
        lambda stagedir, meta, out, sign=True: out.write_bytes(b"test"),
    )
    out, _, _, _ = lpm.run_lpmbuild(
        dst, outdir=tmp_path, prompt_install=False, build_deps=False,
    )
    assert out.is_file()
    assert BASE + "/--KDE--/qt6-base/patch.diff" in urls

    # Remove the temporary sandboxed build paths after validation.
    import shutil
    shutil.rmtree(Path("/tmp/src-qt6-base"), ignore_errors=True)
    shutil.rmtree(Path("/tmp/pkg-qt6-base"), ignore_errors=True)
    shutil.rmtree(Path("/tmp/build-qt6-base"), ignore_errors=True)


def test_cached_category_path_is_used_before_flat_fallback(tmp_path, monkeypatch):
    lpm = _import_lpm(tmp_path, monkeypatch)
    monkeypatch.setitem(lpm.CONF, "LPMBUILD_REPO", BASE)
    entries = [_blob("--KDE--/kwin/kwin.lpmbuild")]
    urls = []

    def reader(url, timeout=10):
        urls.append(url)
        if "/api/v4/projects/" in url:
            return json.dumps(entries).encode(), url
        if url == BASE + "/--KDE--/kwin/kwin.lpmbuild":
            return b"NAME=kwin\\nVERSION=1\\n", url
        raise FileNotFoundError(url)

    monkeypatch.setattr(lpm, "urlread", reader)
    first = tmp_path / "first.lpmbuild"
    lpm.fetch_lpmbuild("kwin", first)
    second = tmp_path / "second.lpmbuild"
    lpm.fetch_lpmbuild("kwin", second)
    assert second.read_bytes() == first.read_bytes()
    assert urls.count(BASE + "/kwin/kwin.lpmbuild") == 1
    assert len([url for url in urls if "/api/v4/projects/" in url]) == 1


def test_flat_recipe_remains_first_and_does_not_query_gitlab_tree(tmp_path, monkeypatch):
    lpm = _import_lpm(tmp_path, monkeypatch)
    monkeypatch.setitem(lpm.CONF, "LPMBUILD_REPO", BASE)
    urls = []

    def reader(url, timeout=10):
        urls.append(url)
        if url == BASE + "/glibc/glibc.lpmbuild":
            return b"NAME=glibc\\nVERSION=2\\n", url
        raise AssertionError(f"unexpected request: {url}")

    monkeypatch.setattr(lpm, "urlread", reader)
    dest = tmp_path / "glibc.lpmbuild"
    lpm.fetch_lpmbuild("glibc", dest)
    assert urls == [BASE + "/glibc/glibc.lpmbuild"]
