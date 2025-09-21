import importlib
import shutil
import sys
import textwrap
import types
from pathlib import Path

import pytest


def _import_lpm(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    for name in ("zstandard", "tqdm"):
        if name not in sys.modules:
            module = types.ModuleType(name)
            if name == "zstandard":
                class _DummyCompressor:
                    def stream_writer(self, fh):
                        return fh

                class _DummyDecompressor:
                    def stream_reader(self, fh):
                        return fh

                module.ZstdCompressor = _DummyCompressor
                module.ZstdDecompressor = _DummyDecompressor
            else:
                class _DummyTqdm:
                    def __init__(self, iterable=None, total=None, **kwargs):
                        self.iterable = iterable or []
                        self.total = total
                        self.n = 0

                    def __iter__(self):
                        for item in self.iterable or []:
                            self.n += 1
                            yield item

                    def update(self, n=1):
                        self.n += n

                    def set_description(self, _desc):
                        return None

                    def __enter__(self):
                        return self

                    def __exit__(self, exc_type, exc, tb):
                        return False

                module.tqdm = _DummyTqdm  # type: ignore[attr-defined]

            sys.modules[name] = module

    for mod in ("lpm", "src.config"):
        if mod in sys.modules:
            del sys.modules[mod]

    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    return importlib.import_module("lpm")


@pytest.fixture
def lpm_module(tmp_path, monkeypatch):
    return _import_lpm(tmp_path, monkeypatch)


def _stub_build_pipeline(lpm, monkeypatch):
    monkeypatch.setattr(lpm, "sandboxed_run", lambda *args, **kwargs: None)
    monkeypatch.setattr(lpm, "generate_install_script", lambda stagedir: "echo hi")

    def fake_build_package(stagedir, meta, out, sign=True):
        out.write_bytes(b"pkg")

    monkeypatch.setattr(lpm, "build_package", fake_build_package)


def test_run_lpmbuild_fetches_relative_sources(lpm_module, tmp_path, monkeypatch):
    lpm = lpm_module
    script = tmp_path / "foo.lpmbuild"
    script.write_text(
        textwrap.dedent(
            """
            NAME=foo
            VERSION=1
            RELEASE=1
            ARCH=noarch
            SOURCE=(
              'https://example.com/dist/foo-1.tar.gz'
              'patch.diff'
            )
            prepare() { :; }
            build() { :; }
            install() { :; }
            """
        )
    )

    _stub_build_pipeline(lpm, monkeypatch)
    monkeypatch.setattr(lpm, "ok", lambda msg: None)
    monkeypatch.setattr(lpm, "warn", lambda msg: None)

    base_repo = "https://repo.example/packages"
    monkeypatch.setitem(lpm.CONF, "LPMBUILD_REPO", base_repo)

    fetched_urls: list[str] = []

    def fake_urlread(url, timeout=10):
        fetched_urls.append(url)
        return b"payload", url

    monkeypatch.setattr(lpm, "urlread", fake_urlread)

    out_path, _, _, _ = lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    assert out_path.exists()
    srcroot = Path("/tmp/src-foo")
    assert (srcroot / "foo-1.tar.gz").read_bytes() == b"payload"
    assert (srcroot / "patch.diff").read_bytes() == b"payload"

    expected_repo_url = f"{base_repo}/foo/patch.diff"
    assert "https://example.com/dist/foo-1.tar.gz" in fetched_urls
    assert expected_repo_url in fetched_urls

    out_path.unlink()
    for suffix in ("pkg-foo", "build-foo", "src-foo"):
        shutil.rmtree(Path(f"/tmp/{suffix}"), ignore_errors=True)


def test_run_lpmbuild_downloads_multiple_url_sources(lpm_module, tmp_path, monkeypatch):
    lpm = lpm_module
    script = tmp_path / "foo.lpmbuild"
    script.write_text(
        textwrap.dedent(
            """
            NAME=foo
            VERSION=1
            RELEASE=1
            ARCH=noarch
            SOURCE=(
              'foo-1.tar.xz::https://downloads.example.com/releases/foo-1.tar.xz'
              'foo-1.tar.xz.sig::https://downloads.example.com/releases/foo-1.tar.xz.sig'
              'patch.diff'
            )
            prepare() { :; }
            build() { :; }
            install() { :; }
            """
        )
    )

    _stub_build_pipeline(lpm, monkeypatch)
    monkeypatch.setattr(lpm, "ok", lambda msg: None)
    monkeypatch.setattr(lpm, "warn", lambda msg: None)

    base_repo = "https://repo.example/packages"
    monkeypatch.setitem(lpm.CONF, "LPMBUILD_REPO", base_repo)

    fetched_urls: list[str] = []

    def fake_urlread(url, timeout=10):
        fetched_urls.append(url)
        return f"payload:{url}".encode(), url

    monkeypatch.setattr(lpm, "urlread", fake_urlread)

    out_path, _, _, _ = lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    assert out_path.exists()
    srcroot = Path("/tmp/src-foo")
    assert (srcroot / "foo-1.tar.xz").read_bytes() == b"payload:https://downloads.example.com/releases/foo-1.tar.xz"
    assert (srcroot / "foo-1.tar.xz.sig").read_bytes() == b"payload:https://downloads.example.com/releases/foo-1.tar.xz.sig"
    assert (srcroot / "patch.diff").read_bytes() == b"payload:https://repo.example/packages/foo/patch.diff"

    expected_urls = [
        "https://downloads.example.com/releases/foo-1.tar.xz",
        "https://downloads.example.com/releases/foo-1.tar.xz.sig",
        "https://repo.example/packages/foo/patch.diff",
    ]
    assert fetched_urls == expected_urls

    out_path.unlink()
    for suffix in ("pkg-foo", "build-foo", "src-foo"):
        shutil.rmtree(Path(f"/tmp/{suffix}"), ignore_errors=True)


def test_run_lpmbuild_allows_alias_for_repo_sources(lpm_module, tmp_path, monkeypatch):
    lpm = lpm_module
    script = tmp_path / "foo.lpmbuild"
    script.write_text(
        textwrap.dedent(
            """
            NAME=foo
            VERSION=1
            RELEASE=1
            ARCH=noarch
            SOURCE=(
              'renamed.patch::patch.diff'
            )
            prepare() { :; }
            build() { :; }
            install() { :; }
            """
        )
    )

    _stub_build_pipeline(lpm, monkeypatch)
    monkeypatch.setattr(lpm, "ok", lambda msg: None)
    monkeypatch.setattr(lpm, "warn", lambda msg: None)

    base_repo = "https://repo.example/packages"
    monkeypatch.setitem(lpm.CONF, "LPMBUILD_REPO", base_repo)

    fetched_urls: list[str] = []

    def fake_urlread(url, timeout=10):
        fetched_urls.append(url)
        return b"payload", url

    monkeypatch.setattr(lpm, "urlread", fake_urlread)

    out_path, _, _, _ = lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    assert out_path.exists()
    srcroot = Path("/tmp/src-foo")
    assert (srcroot / "renamed.patch").read_bytes() == b"payload"
    expected_repo_url = f"{base_repo}/foo/patch.diff"
    assert fetched_urls == [expected_repo_url]

    out_path.unlink()
    for suffix in ("pkg-foo", "build-foo", "src-foo"):
        shutil.rmtree(Path(f"/tmp/{suffix}"), ignore_errors=True)


def test_run_lpmbuild_skips_metadata_url_fetch(lpm_module, tmp_path, monkeypatch):
    lpm = lpm_module
    script = tmp_path / "foo.lpmbuild"
    script.write_text(
        textwrap.dedent(
            """
            NAME=foo
            VERSION=1
            RELEASE=1
            ARCH=noarch
            URL=https://unreachable.example.invalid/project
            SOURCE=(
              'https://downloads.example.com/foo-1.tar.gz'
            )
            prepare() { :; }
            build() { :; }
            install() { :; }
            """
        )
    )

    _stub_build_pipeline(lpm, monkeypatch)
    monkeypatch.setattr(lpm, "ok", lambda msg: None)
    monkeypatch.setattr(lpm, "warn", lambda msg: None)

    fetched_urls: list[str] = []

    def fake_urlread(url, timeout=10):
        if "unreachable" in url:
            raise AssertionError("metadata URL should not be fetched")
        fetched_urls.append(url)
        return b"payload", url

    monkeypatch.setattr(lpm, "urlread", fake_urlread)

    out_path, _, _, _ = lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    assert out_path.exists()
    srcroot = Path("/tmp/src-foo")
    assert (srcroot / "foo-1.tar.gz").read_bytes() == b"payload"
    assert fetched_urls == ["https://downloads.example.com/foo-1.tar.gz"]

    out_path.unlink()
    for suffix in ("pkg-foo", "build-foo", "src-foo"):
        shutil.rmtree(Path(f"/tmp/{suffix}"), ignore_errors=True)


def test_maybe_fetch_source_skips_existing_file(lpm_module, tmp_path, monkeypatch):
    lpm = lpm_module
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    target = src_dir / "already.patch"
    target.write_text("present", encoding="utf-8")

    def fake_urlread(url, timeout=10):
        raise AssertionError("should not refetch existing sources")

    monkeypatch.setattr(lpm, "urlread", fake_urlread)

    lpm._maybe_fetch_source("https://example.com/already.patch", src_dir, filename="already.patch")

    assert target.read_text(encoding="utf-8") == "present"
