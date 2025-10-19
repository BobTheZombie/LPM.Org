import os
import subprocess
import sys
import textwrap
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if "tqdm" not in sys.modules:
    module = types.ModuleType("tqdm")

    class _DummyTqdm:
        def __init__(self, iterable=None, **_kwargs):
            self.iterable = iterable or []
            self.n = 0

        def __iter__(self):
            for item in self.iterable:
                yield item

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def update(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    module.tqdm = _DummyTqdm
    sys.modules["tqdm"] = module

import lpm


def _prepare_fake_pip(monkeypatch, metadata_text, *, create_native=False):
    original_run = subprocess.run
    python_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"

    dist_name = "demo"
    dist_version = "1.0"
    for line in metadata_text.splitlines():
        if line.startswith("Name:"):
            dist_name = line.split(":", 1)[1].strip() or dist_name
        elif line.startswith("Version:"):
            dist_version = line.split(":", 1)[1].strip() or dist_version
    sdist_name = f"{dist_name}-{dist_version}.tar.gz"

    canonical_pkg = dist_name.lower().replace("-", "_")

    def _handle_install(cmd):
        sdist_arg = next((arg for arg in cmd if str(arg).endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".zip"))), None)
        assert sdist_arg, "pip install should receive local sdist path"
        assert Path(sdist_arg).name == sdist_name
        root_idx = cmd.index("--root")
        root = Path(cmd[root_idx + 1])
        site = root / "usr" / "lib" / python_ver / "site-packages"
        site.mkdir(parents=True, exist_ok=True)
        dist_info = site / f"{canonical_pkg}-{dist_version}.dist-info"
        dist_info.mkdir(parents=True, exist_ok=True)
        (dist_info / "METADATA").write_text(metadata_text, encoding="utf-8")
        pkg_dir = site / canonical_pkg
        pkg_dir.mkdir(parents=True, exist_ok=True)
        (pkg_dir / "__init__.py").write_text("__all__ = []\n", encoding="utf-8")
        if create_native:
            native = pkg_dir / "native.so"
            native.write_bytes(b"\x7fELF")
            native.chmod(0o755)
        bin_dir = root / "usr" / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        script_path = bin_dir / "demo"
        script_path.write_text("#!/bin/sh\necho demo\n", encoding="utf-8")
        script_path.chmod(0o755)
        return subprocess.CompletedProcess(cmd, 0)

    def fake_run(cmd, *args, **kwargs):
        if (
            isinstance(cmd, (list, tuple))
            and len(cmd) >= 4
            and cmd[1] == "-m"
            and cmd[2] == "pip"
        ):
            if cmd[3] == "download":
                dest_idx = cmd.index("--dest")
                dest = Path(cmd[dest_idx + 1])
                dest.mkdir(parents=True, exist_ok=True)
                (dest / sdist_name).write_bytes(b"sdist")
                return subprocess.CompletedProcess(cmd, 0)
            if cmd[3] == "install":
                return _handle_install(cmd)
        return original_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)


@pytest.fixture(autouse=True)
def _ensure_state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def test_cmd_buildpkg_python_pip_generates_metadata(monkeypatch, tmp_path):
    metadata = textwrap.dedent(
        """
        Metadata-Version: 2.1
        Name: Demo
        Version: 1.2
        Summary: Demo package
        Home-page: https://example.com/demo
        License: MIT
        Requires-Python: >=3.8
        Requires-Dist: Requests (>=2)
        """
    ).strip()
    _prepare_fake_pip(monkeypatch, metadata)

    recorded = []

    def _record_prompt(blob, **kwargs):
        recorded.append((blob, kwargs.get("root")))

    monkeypatch.setattr(lpm, "prompt_install_pkg", _record_prompt)

    args = SimpleNamespace(
        script=None,
        python_pip="demo==1.2",
        outdir=tmp_path,
        no_deps=False,
        install_default=None,
        force_rebuild=False,
        root=tmp_path / "install-root",
    )
    lpm.cmd_buildpkg(args)

    built = tmp_path / "python-demo-1.2-1.noarch.zst"
    assert built.exists()
    assert recorded and recorded[0] == (built, args.root)

    meta, _ = lpm.read_package_meta(built)
    assert meta.name == "python-demo"
    assert meta.version == "1.2"
    assert meta.arch == "noarch"
    assert meta.summary == "Demo package"
    assert meta.url == "https://example.com/demo"
    assert meta.license == "MIT"
    assert "python>=3.8" in meta.requires
    assert "python-requests>=2" in meta.requires
    assert "pypi(demo)" in meta.provides


def test_cmd_buildpkg_python_pip_respects_no_deps(monkeypatch, tmp_path):
    metadata = textwrap.dedent(
        """
        Metadata-Version: 2.1
        Name: Demo
        Version: 1.2
        Requires-Python: >=3.10
        Requires-Dist: requests (>=2)
        """
    ).strip()
    _prepare_fake_pip(monkeypatch, metadata)

    monkeypatch.setattr(lpm, "prompt_install_pkg", lambda *args, **kwargs: None)

    args = SimpleNamespace(
        script=None,
        python_pip="demo",
        outdir=tmp_path,
        no_deps=True,
        install_default=None,
        force_rebuild=False,
        root=None,
    )
    lpm.cmd_buildpkg(args)

    built = tmp_path / "python-demo-1.2-1.noarch.zst"
    meta, _ = lpm.read_package_meta(built)
    assert meta.requires == ["python>=3.10"]


def test_cmd_buildpkg_python_pip_native_arch(monkeypatch, tmp_path):
    metadata = textwrap.dedent(
        """
        Metadata-Version: 2.1
        Name: Demo
        Version: 1.2
        """
    ).strip()
    _prepare_fake_pip(monkeypatch, metadata, create_native=True)
    monkeypatch.setattr(lpm, "prompt_install_pkg", lambda *args, **kwargs: None)

    args = SimpleNamespace(
        script=None,
        python_pip="demo",
        outdir=tmp_path,
        no_deps=True,
        install_default=None,
        force_rebuild=False,
        root=None,
    )
    lpm.cmd_buildpkg(args)

    built = tmp_path / f"python-demo-1.2-1.{lpm.ARCH or os.uname().machine}.zst"
    assert built.exists()
    meta, _ = lpm.read_package_meta(built)
    expected_arch = lpm.ARCH or (os.uname().machine if hasattr(os, "uname") else "") or "noarch"
    assert meta.arch == expected_arch


def test_cmd_buildpkg_python_pip_preserves_existing_python_prefix(monkeypatch, tmp_path):
    metadata = textwrap.dedent(
        """
        Metadata-Version: 2.1
        Name: python-dateutil
        Version: 2.9.0
        """
    ).strip()
    _prepare_fake_pip(monkeypatch, metadata)
    monkeypatch.setattr(lpm, "prompt_install_pkg", lambda *args, **kwargs: None)

    args = SimpleNamespace(
        script=None,
        python_pip="python-dateutil",
        outdir=tmp_path,
        no_deps=True,
        install_default=None,
        force_rebuild=False,
    )
    lpm.cmd_buildpkg(args)

    built = tmp_path / "python-dateutil-2.9.0-1.noarch.zst"
    assert built.exists()
    meta, _ = lpm.read_package_meta(built)
    assert meta.name == "python-dateutil"


def test_cmd_buildpkg_python_pip_falls_back_to_python_from_which(monkeypatch, tmp_path):
    metadata = textwrap.dedent(
        """
        Metadata-Version: 2.1
        Name: Demo
        Version: 1.2
        """
    ).strip()
    _prepare_fake_pip(monkeypatch, metadata)
    monkeypatch.setattr(lpm, "prompt_install_pkg", lambda *args, **kwargs: None)

    missing = tmp_path / "missing-python"
    monkeypatch.setattr(lpm.sys, "executable", str(missing))

    fallback = tmp_path / "bin" / "python3"
    fallback.parent.mkdir(parents=True, exist_ok=True)
    fallback.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fallback.chmod(0o755)

    original_which = lpm.shutil.which

    def fake_which(name):
        if name == "python3":
            return str(fallback)
        return original_which(name)

    monkeypatch.setattr(lpm.shutil, "which", fake_which)

    args = SimpleNamespace(
        script=None,
        python_pip="demo",
        outdir=tmp_path,
        no_deps=True,
        install_default=None,
        force_rebuild=False,
    )
    lpm.cmd_buildpkg(args)

    built = tmp_path / "python-demo-1.2-1.noarch.zst"
    assert built.exists()
