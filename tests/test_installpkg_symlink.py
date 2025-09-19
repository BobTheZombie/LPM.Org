import json
import os
import sys
import types
import shutil
import importlib
import dataclasses
import tarfile
from pathlib import Path

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


def test_installpkg_verifies_symlink_manifest(tmp_path, monkeypatch):
    lpm = _import_lpm(tmp_path, monkeypatch)
    root = tmp_path / "root"
    root.mkdir()

    pkg = _make_symlink_pkg(lpm, tmp_path)

    lpm.installpkg(pkg, root=root, dry_run=False, verify=False, force=False, explicit=True)

    installed_link = root / "link"
    assert installed_link.is_symlink()
    assert os.readlink(installed_link) == "target"
