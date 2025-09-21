import contextlib
import importlib
import os
import shlex
import sys
import textwrap
from pathlib import Path

import pytest


def _write_stub_modules(stub_dir: Path) -> None:
    stub_dir.mkdir(parents=True, exist_ok=True)
    (stub_dir / "zstandard.py").write_text(
        textwrap.dedent(
            """
            class ZstdCompressor:
                def stream_writer(self, fh):
                    return fh

            class ZstdDecompressor:
                def stream_reader(self, fh):
                    return fh
            """
        ),
        encoding="utf-8",
    )
    (stub_dir / "tqdm.py").write_text(
        textwrap.dedent(
            """
            class tqdm:
                def __init__(self, iterable=None, **kwargs):
                    self.iterable = iterable or []
                    self.total = kwargs.get("total")
                    self.desc = kwargs.get("desc")
                    self.n = 0

                def __iter__(self):
                    for item in self.iterable:
                        self.n += 1
                        yield item

                def update(self, n=1):
                    self.n += n

                def set_description(self, desc):
                    self.desc = desc

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False
            """
        ),
        encoding="utf-8",
    )


def _import_lpm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    stub_dir = tmp_path / "stubs"
    _write_stub_modules(stub_dir)

    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    new_pythonpath = os.pathsep.join(filter(None, [str(stub_dir), existing_pythonpath]))
    monkeypatch.setenv("PYTHONPATH", new_pythonpath)
    if str(stub_dir) not in sys.path:
        sys.path.insert(0, str(stub_dir))

    for mod in ("zstandard", "tqdm", "lpm", "src.config"):
        sys.modules.pop(mod, None)

    return importlib.import_module("lpm")


@pytest.fixture
def lpm_module(tmp_path, monkeypatch):
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    return _import_lpm(tmp_path, monkeypatch)


def test_run_lpmbuild_creates_split_packages(tmp_path, lpm_module):
    script = tmp_path / "split.lpmbuild"
    script.write_text(
        "\n".join(
            [
                "NAME=foo",
                "VERSION=1.2.3",
                "RELEASE=2",
                "ARCH=noarch",
                "SUMMARY=\"Base package\"",
                "prepare(){ :; }",
                "build(){ :; }",
                "install(){",
                "  mkdir -p \"$pkgdir/usr/bin\"",
                "  echo base > \"$pkgdir/usr/bin/foo\"",
                "  split_a=\"$BUILDROOT/split-a\"",
                "  mkdir -p \"$split_a/usr/bin\"",
                "  echo alpha > \"$split_a/usr/bin/foo-alpha\"",
                "  $LPM_SPLIT_PACKAGE --stagedir \"$split_a\" --name foo-alpha --summary 'Alpha compiler' --requires bar",
                "  split_b=\"$BUILDROOT/split-b\"",
                "  mkdir -p \"$split_b/usr/bin\"",
                "  echo beta > \"$split_b/usr/bin/foo-beta\"",
                "  $LPM_SPLIT_PACKAGE --stagedir \"$split_b\" --name foo-beta --provides foo-beta-bin",
                "}",
            ]
        )
    )

    out_path, _, _, splits = lpm_module.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    assert out_path.exists()
    assert len(splits) == 2
    names = sorted(meta.name for _, meta in splits)
    assert names == ["foo-alpha", "foo-beta"]
    for path, meta in splits:
        assert path.exists()
        assert meta.version == "1.2.3"
        assert meta.release == "2"
        if meta.name == "foo-alpha":
            assert meta.requires == ["bar"]
            assert meta.summary == "Alpha compiler"
        if meta.name == "foo-beta":
            assert meta.provides == ["foo-beta-bin"]


def test_split_package_helper_falls_back_to_argv0(tmp_path, monkeypatch, lpm_module):
    script = tmp_path / "split.lpmbuild"
    script.write_text(
        "\n".join(
            [
                "NAME=foo",
                "VERSION=1.0.0",
                "RELEASE=1",
                "ARCH=noarch",
                "SUMMARY=\"Base package\"",
                "prepare(){ :; }",
                "build(){ :; }",
                "install(){",
                "  mkdir -p \"$pkgdir/usr/bin\"",
                "  echo base > \"$pkgdir/usr/bin/foo\"",
                "}",
            ]
        )
    )

    fallback_binary = tmp_path / "fake-lpm"
    fallback_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fallback_binary.chmod(0o755)

    missing_executable = tmp_path / "missing-python"
    monkeypatch.setattr(sys, "executable", str(missing_executable))
    monkeypatch.setattr(sys, "argv", [str(fallback_binary)] + sys.argv[1:])

    helper_path = Path("/tmp/build-foo/lpm-split-package")
    original_unlink = Path.unlink
    with contextlib.suppress(FileNotFoundError):
        original_unlink(helper_path)

    def fake_unlink(self, *args, **kwargs):
        if self == helper_path:
            return None
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fake_unlink)

    lpm_module.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    helper_contents = helper_path.read_text(encoding="utf-8")
    expected = shlex.quote(str(fallback_binary.resolve()))
    assert expected in helper_contents
    assert str(missing_executable) not in helper_contents

    original_unlink(helper_path)


def test_split_package_helper_for_frozen_executable(tmp_path, monkeypatch, lpm_module):
    script = tmp_path / "split.lpmbuild"
    script.write_text(
        "\n".join(
            [
                "NAME=foo",
                "VERSION=1.0.0",
                "RELEASE=1",
                "ARCH=noarch",
                "SUMMARY=\"Base package\"",
                "prepare(){ :; }",
                "build(){ :; }",
                "install(){",
                "  mkdir -p \"$pkgdir/usr/bin\"",
                "  echo base > \"$pkgdir/usr/bin/foo\"",
                "}",
            ]
        )
    )

    frozen_binary = tmp_path / "frozen-lpm"
    frozen_binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    frozen_binary.chmod(0o755)

    current_argv = list(sys.argv)
    monkeypatch.setattr(sys, "executable", str(frozen_binary))
    monkeypatch.setattr(sys, "argv", [str(frozen_binary)] + current_argv[1:])
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    helper_path = Path("/tmp/build-foo/lpm-split-package")
    original_unlink = Path.unlink
    with contextlib.suppress(FileNotFoundError):
        original_unlink(helper_path)

    def fake_unlink(self, *args, **kwargs):
        if self == helper_path:
            return None
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fake_unlink)

    lpm_module.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    helper_contents = helper_path.read_text(encoding="utf-8")
    expected = f"exec {shlex.quote(str(frozen_binary.resolve()))} splitpkg \"$@\""
    assert expected in helper_contents
    module_path = Path(lpm_module.__file__).resolve()
    assert str(module_path) not in helper_contents

    original_unlink(helper_path)
