import json
import os
import sys
import shutil
import importlib
import dataclasses
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _import_lpm(tmp_path, monkeypatch):
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    if "tqdm" not in sys.modules:
        import types

        module = types.ModuleType("tqdm")

        class _DummyTqdm:
            def __init__(self, iterable=None, **kwargs):
                self.iterable = iterable
                self.n = 0
                self.total = kwargs.get("total")

            def __iter__(self):
                return iter(self.iterable or [])

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def update(self, *args, **kwargs):
                return None

        module.tqdm = _DummyTqdm  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "tqdm", module)
    for mod in ["lpm", "src.config"]:
        if mod in sys.modules:
            del sys.modules[mod]
    return importlib.import_module("lpm")


def _make_symlink_pkg(lpm, tmp_path):
    staged = tmp_path / "stage-symlink"
    staged.mkdir()
    link = staged / "link"
    link.symlink_to("target")

    manifest = lpm.collect_manifest(staged)
    assert any("link" in entry for entry in manifest)
    meta = lpm.PkgMeta(name="symlink", version="1", release="1", arch="noarch")

    (staged / ".lpm-meta.json").write_text(json.dumps(dataclasses.asdict(meta)))
    (staged / ".lpm-manifest.json").write_text(json.dumps(manifest))

    out = tmp_path / "symlink.zst"
    with out.open("wb") as f:
        cctx = lpm.zstd.ZstdCompressor()
        with cctx.stream_writer(f) as compressor:
            with tarfile.open(fileobj=compressor, mode="w|") as tf:
                for p in staged.iterdir():
                    tf.add(p, arcname=p.name)

    shutil.rmtree(staged)
    return out


def _make_ldso_pkg(lpm, tmp_path):
    staged = tmp_path / "stage-ldso"
    (staged / "usr/bin").mkdir(parents=True)
    (staged / "usr/lib").mkdir(parents=True)

    loader = staged / "usr/lib/ld-2.38.so"
    loader.write_bytes(b"fake-elf\x00")

    ld_so = staged / "usr/bin/ld.so"
    ld_so.symlink_to("../lib/ld-2.38.so")

    manifest = lpm.collect_manifest(staged)
    loader_hash = lpm.sha256sum(loader)
    for entry in manifest:
        if entry["path"] == "/usr/bin/ld.so":
            entry["sha256"] = loader_hash
            break
    else:
        raise AssertionError("ld.so entry missing from manifest")

    meta = lpm.PkgMeta(name="glibc-test", version="1", release="1", arch="x86_64")

    (staged / ".lpm-meta.json").write_text(json.dumps(dataclasses.asdict(meta)))
    (staged / ".lpm-manifest.json").write_text(json.dumps(manifest))

    out = tmp_path / "glibc-ldso.zst"
    with out.open("wb") as f:
        cctx = lpm.zstd.ZstdCompressor()
        with cctx.stream_writer(f) as compressor:
            with tarfile.open(fileobj=compressor, mode="w|") as tf:
                for p in staged.iterdir():
                    tf.add(p, arcname=p.name)

    shutil.rmtree(staged)
    return out


def _make_absolute_symlink_pkg(lpm, tmp_path):
    staged = tmp_path / "stage-absolute"
    (staged / "usr/bin").mkdir(parents=True)
    (staged / "usr/lib").mkdir(parents=True)

    payload = staged / "usr/lib/libabsolute.so"
    payload.write_bytes(b"absolute")

    link = staged / "usr/bin/libabsolute.so"
    link.symlink_to("/usr/lib/libabsolute.so")

    install_script = staged / ".lpm-install.sh"
    script_body = lpm.generate_install_script(staged)
    install_script.write_text("#!/bin/sh\nset -e\n" + script_body + "\n")
    install_script.chmod(0o755)

    manifest = lpm.collect_manifest(staged)
    meta = lpm.PkgMeta(name="absolute-symlink", version="1", release="1", arch="noarch")

    (staged / ".lpm-meta.json").write_text(json.dumps(dataclasses.asdict(meta)))
    (staged / ".lpm-manifest.json").write_text(json.dumps(manifest))

    out = tmp_path / "absolute-symlink.zst"
    with out.open("wb") as f:
        cctx = lpm.zstd.ZstdCompressor()
        with cctx.stream_writer(f) as compressor:
            with tarfile.open(fileobj=compressor, mode="w|") as tf:
                for p in staged.iterdir():
                    tf.add(p, arcname=p.name)

    shutil.rmtree(staged)
    return out


def _make_conflict_pkg(lpm, tmp_path):
    staged = tmp_path / "stage-conflict"
    (staged / "etc").mkdir(parents=True)

    (staged / "etc" / "foo").write_text("package foo\n")
    (staged / "etc" / "bar").write_text("package bar\n")

    manifest = lpm.collect_manifest(staged)
    meta = lpm.PkgMeta(name="conflict", version="1", release="1", arch="noarch")

    (staged / ".lpm-meta.json").write_text(json.dumps(dataclasses.asdict(meta)))
    (staged / ".lpm-manifest.json").write_text(json.dumps(manifest))

    out = tmp_path / "conflict.zst"
    with out.open("wb") as f:
        cctx = lpm.zstd.ZstdCompressor()
        with cctx.stream_writer(f) as compressor:
            with tarfile.open(fileobj=compressor, mode="w|") as tf:
                for p in staged.iterdir():
                    tf.add(p, arcname=p.name)

    shutil.rmtree(staged)
    return out


@pytest.fixture
def lpm_module(tmp_path, monkeypatch):
    return _import_lpm(tmp_path, monkeypatch)


@pytest.fixture
def ldso_package(tmp_path, lpm_module):
    return _make_ldso_pkg(lpm_module, tmp_path)


@pytest.fixture
def absolute_symlink_package(tmp_path, lpm_module):
    return _make_absolute_symlink_pkg(lpm_module, tmp_path)


def test_installpkg_verifies_symlink_manifest(tmp_path, monkeypatch):
    lpm = _import_lpm(tmp_path, monkeypatch)
    root = tmp_path / "root"
    root.mkdir()

    pkg = _make_symlink_pkg(lpm, tmp_path)

    lpm.installpkg(pkg, root=root, dry_run=False, verify=False, force=False, explicit=True)

    installed_link = root / "link"
    assert installed_link.is_symlink()
    assert os.readlink(installed_link) == "target"


def test_installpkg_accepts_file_digest_for_symlink(lpm_module, ldso_package, tmp_path):
    root = tmp_path / "root-glibc"
    root.mkdir()

    lpm_module.installpkg(ldso_package, root=root, dry_run=False, verify=False, force=False, explicit=True)

    installed_ld = root / "usr/bin/ld.so"
    assert installed_ld.is_symlink()
    assert os.readlink(installed_ld) == "../lib/ld-2.38.so"
    loader = root / "usr/lib/ld-2.38.so"
    assert loader.is_file()


def test_installpkg_recreates_absolute_symlink(lpm_module, absolute_symlink_package, tmp_path):
    root = tmp_path / "root-absolute"
    root.mkdir()

    lpm_module.installpkg(
        absolute_symlink_package,
        root=root,
        dry_run=False,
        verify=False,
        force=False,
        explicit=True,
    )

    installed_link = root / "usr/bin/libabsolute.so"
    assert installed_link.is_symlink()
    assert os.readlink(installed_link) == "../lib/libabsolute.so"
    assert (root / "usr/lib/libabsolute.so").is_file()


def test_installpkg_replace_all_option(tmp_path, monkeypatch):
    lpm = _import_lpm(tmp_path, monkeypatch)
    root = tmp_path / "root-conflict"
    (root / "etc").mkdir(parents=True)

    (root / "etc" / "foo").write_text("package foo\n")
    foo_inode_before = os.stat(root / "etc" / "foo").st_ino
    (root / "etc" / "bar").write_text("existing bar\n")

    pkg = _make_conflict_pkg(lpm, tmp_path)

    prompts = []

    responses = iter(["ra"])

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        try:
            return next(responses)
        except StopIteration:  # pragma: no cover - ensures unexpected prompts fail the test
            raise AssertionError("Unexpected additional prompt")

    monkeypatch.setattr("builtins.input", fake_input)

    lpm.installpkg(pkg, root=root, dry_run=False, verify=False, force=False, explicit=True)

    assert len(prompts) == 1
    assert "[RA] Replace All" in prompts[0]

    assert (root / "etc" / "foo").read_text() == "package foo\n"
    assert (root / "etc" / "bar").read_text() == "package bar\n"
    assert os.stat(root / "etc" / "foo").st_ino != foo_inode_before
