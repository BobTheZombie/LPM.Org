import json
import os
import sys
import types
import shutil
import importlib
import dataclasses
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _import_lpm(tmp_path, monkeypatch):
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    for name in ("zstandard", "tqdm"):
        if name not in sys.modules:
            module = types.ModuleType(name)
            if name == "zstandard":
                class _StreamWriter:
                    def __init__(self, fh):
                        self._fh = fh
                        self._started = False

                    def write(self, data):
                        if not self._started:
                            self._fh.write(b"\x28\xb5\x2f\xfd")
                            self._started = True
                        return self._fh.write(data)

                    def flush(self):
                        return self._fh.flush()

                    def close(self):
                        return None

                    def __enter__(self):
                        if not self._started:
                            self._fh.write(b"\x28\xb5\x2f\xfd")
                            self._started = True
                        return self

                    def __exit__(self, exc_type, exc, tb):
                        return False

                class _Compressor:
                    def stream_writer(self, fh):
                        return _StreamWriter(fh)

                class _Decompressor:
                    def stream_reader(self, fh):
                        class _Reader:
                            def __init__(self, inner):
                                self._inner = inner
                                self._skipped = False

                            def read(self, size=-1):
                                if not self._skipped:
                                    self._inner.read(4)
                                    self._skipped = True
                                return self._inner.read(size)

                            def close(self):
                                return self._inner.close()

                            def readable(self):
                                return True

                        return _Reader(fh)

                module.ZstdCompressor = _Compressor
                module.ZstdDecompressor = _Decompressor
            else:
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
            monkeypatch.setitem(sys.modules, name, module)
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


@pytest.fixture
def lpm_module(tmp_path, monkeypatch):
    return _import_lpm(tmp_path, monkeypatch)


@pytest.fixture
def ldso_package(tmp_path, lpm_module):
    return _make_ldso_pkg(lpm_module, tmp_path)


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
