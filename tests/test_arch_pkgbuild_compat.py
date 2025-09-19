import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import arch_compat


PKGBUILD_SAMPLE = """
# Maintainer: Example
pkgname=foo
pkgver=1.2
pkgrel=3
pkgdesc="Demo package"
arch=('any')
url='https://example.com/foo'
license=('MIT')
depends=('bar>=1.0')
makedepends=('cmake')
optdepends=('baz:extra tool')
provides=('foo')
conflicts=('foo-old')
replaces=('foo-old')
source=('foo.tar.gz')

prepare() {
    pacman -S --noconfirm something
    echo prepare
}

build() {
    echo build
}

package() {
    pacman -Rsn something
    mkdir -p "$pkgdir/usr/bin"
    echo hi > "$pkgdir/usr/bin/foo"
}
"""


def test_convert_pkgbuild_strips_pacman_calls():
    info, script = arch_compat.convert_pkgbuild_to_lpmbuild(PKGBUILD_SAMPLE)

    assert info.name == "foo"
    assert info.version == "1.2"
    assert info.release == "3"
    assert "pacman" not in script
    assert "REQUIRES=(" in script
    assert "'bar>=1.0'" in script
    assert "cmake" in script
    assert "SUGGESTS=(baz)" in script
    assert "install()" in script
    assert "mkdir -p" in script
    assert info.dependency_names() == ["bar", "cmake"]


def test_converter_fetches_dependencies(tmp_path):
    foo_pkgbuild = """
    pkgname=foo
    pkgver=1
    pkgrel=1
    pkgdesc='Foo'
    depends=('bar')
    makedepends=()
    build() { :; }
    package() {
        mkdir -p "$pkgdir/usr"
        touch "$pkgdir/usr/foo"
    }
    """
    bar_pkgbuild = """
    pkgname=bar
    pkgver=1
    pkgrel=1
    pkgdesc='Bar'
    build() { :; }
    package() {
        mkdir -p "$pkgdir/usr"
        touch "$pkgdir/usr/bar"
    }
    """

    registry = {"foo": foo_pkgbuild, "bar": bar_pkgbuild}

    def fake_fetch(name: str) -> str:
        return registry[name]

    converter = arch_compat.PKGBuildConverter(tmp_path, fetcher=fake_fetch)
    info, script_path = converter.convert_text(registry["foo"])
    assert script_path.exists()
    assert info.dependency_names() == ["bar"]

    fetcher = converter.make_fetcher()
    dest = tmp_path / "bar.lpmbuild"
    fetcher("bar", dest)
    text = dest.read_text(encoding="utf-8")
    assert "NAME=bar" in text
    assert "install()" in text


def test_meta_package_conversion():
    meta_pkgbuild = """
    pkgname=base-devel
    pkgver=1
    pkgrel=1
    pkgdesc='Meta'
    arch=('any')
    depends=('gcc' 'make')
    package() { :; }
    """

    info, script = arch_compat.convert_pkgbuild_to_lpmbuild(meta_pkgbuild)
    assert info.dependency_names() == ["gcc", "make"]
    assert "install()" in script
    assert "    :" in script


def _load_lpm(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parent.parent
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LPM_DEVELOPER_MODE", "1")
    for name in ["lpm", "src.config", "src.arch_compat"]:
        sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location("lpm", root / "lpm.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["lpm"] = module
    spec.loader.exec_module(module)
    return module


def test_buildpkg_from_pkgbuild(tmp_path, monkeypatch):
    lpm = _load_lpm(tmp_path, monkeypatch)
    pkgbuild = tmp_path / "PKGBUILD"
    pkgbuild.write_text(
        """
        pkgname=testpkg
        pkgver=1
        pkgrel=1
        pkgdesc='Demo'
        build() { :; }
        package() {
            mkdir -p "$pkgdir/usr/bin"
            echo hi > "$pkgdir/usr/bin/hi"
        }
        """,
        encoding="utf-8",
    )

    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: "n")

    args = SimpleNamespace(
        script=pkgbuild,
        outdir=tmp_path,
        no_deps=True,
        install_default="n",
        from_pkgbuild=True,
    )

    lpm.cmd_buildpkg(args)
    produced = list(tmp_path.glob("testpkg-1-1.*.zst"))
    assert produced


def test_pkgbuild_cli_build_with_dependencies(tmp_path, monkeypatch):
    lpm = _load_lpm(tmp_path, monkeypatch)
    from src import arch_compat as converter_module  # type: ignore

    foo_pkgbuild = """
    pkgname=foo
    pkgver=1
    pkgrel=1
    pkgdesc='Foo'
    depends=('bar')
    build() { :; }
    package() {
        mkdir -p "$pkgdir/usr/bin"
        echo foo > "$pkgdir/usr/bin/foo"
    }
    """
    bar_pkgbuild = """
    pkgname=bar
    pkgver=1
    pkgrel=1
    pkgdesc='Bar'
    build() { :; }
    package() {
        mkdir -p "$pkgdir/usr/bin"
        echo bar > "$pkgdir/usr/bin/bar"
    }
    """

    mapping = {"bar": bar_pkgbuild}

    def fake_fetch(name: str, endpoints=None):
        if name not in mapping:
            raise RuntimeError(name)
        return mapping[name]

    monkeypatch.setattr(converter_module, "fetch_pkgbuild", fake_fetch)
    monkeypatch.setattr("builtins.input", lambda *args, **kwargs: "n")

    source = tmp_path / "PKGBUILD"
    source.write_text(foo_pkgbuild, encoding="utf-8")

    args = SimpleNamespace(
        source=str(source),
        output=tmp_path / "foo.lpmbuild",
        build=True,
        install=False,
        outdir=tmp_path,
        no_deps=False,
        install_default="n",
    )

    lpm.cmd_pkgbuild_to_lpmbuild(args)

    assert args.output.exists()
    built = list(tmp_path.glob("foo-1-1.*.zst"))
    assert built
    dep_pkgs = list(tmp_path.glob("bar-1-1.*.zst"))
    assert dep_pkgs
