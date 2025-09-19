from __future__ import annotations

import importlib.util
import json
import sys
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_lpm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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


def test_pkgbuild_export_tarball(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    lpm = _load_lpm(tmp_path, monkeypatch)

    pkgbuilds = {
        "foo": """
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
        """,
        "bar": """
            pkgname=bar
            pkgver=1
            pkgrel=1
            pkgdesc='Bar'
            build() { :; }
            package() {
                mkdir -p "$pkgdir/usr/bin"
                echo bar > "$pkgdir/usr/bin/bar"
            }
        """,
        "meta-pkg": """
            pkgname=meta-pkg
            pkgver=1
            pkgrel=1
            pkgdesc='Meta'
            arch=('any')
            depends=('foo' 'baz')
            package() { :; }
        """,
        "baz": """
            pkgname=baz
            pkgver=1
            pkgrel=1
            pkgdesc='Baz'
            build() { :; }
            package() {
                mkdir -p "$pkgdir/usr/bin"
                echo baz > "$pkgdir/usr/bin/baz"
            }
        """,
    }

    def fake_fetch(name: str, endpoints=None):
        if name not in pkgbuilds:
            raise RuntimeError(name)
        return pkgbuilds[name]

    monkeypatch.setattr("src.arch_compat.fetch_pkgbuild", fake_fetch)

    index_path = tmp_path / "index.json"
    index_path.write_text(json.dumps({"packages": [{"name": "meta-pkg"}]}), encoding="utf-8")

    out_tar = tmp_path / "export.tar"
    args = SimpleNamespace(output=out_tar, targets=["foo", str(index_path)], workspace=None)

    lpm.cmd_pkgbuild_export_tar(args)

    assert out_tar.exists()

    with tarfile.open(out_tar, "r") as tf:
        members = {m.name for m in tf.getmembers()}
        expected = {
            "packages",
            "packages/foo",
            "packages/foo/foo.lpmbuild",
            "packages/bar",
            "packages/bar/bar.lpmbuild",
            "packages/meta-pkg",
            "packages/meta-pkg/meta-pkg.lpmbuild",
            "packages/baz",
            "packages/baz/baz.lpmbuild",
        }
        assert expected.issubset(members)

        meta_file = tf.extractfile("packages/meta-pkg/meta-pkg.lpmbuild")
        assert meta_file is not None
        meta_text = meta_file.read().decode("utf-8")
        assert "NAME=meta-pkg" in meta_text
        assert "install()" in meta_text
        assert "    :" in meta_text

        foo_file = tf.extractfile("packages/foo/foo.lpmbuild")
        assert foo_file is not None
        foo_text = foo_file.read().decode("utf-8")
        assert "REQUIRES=(bar" in foo_text
