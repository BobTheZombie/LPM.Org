from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from lpm import chroot_helpers
from lpm import app


def _write_script(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n", encoding="utf-8")


def test_buildgen_manifest_deterministic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "packages"
    output = tmp_path / "out"
    a = source / "alpha" / "alpha.lpmbuild"
    b = source / "beta" / "beta.lpmbuild"
    _write_script(a)
    _write_script(b)

    meta = {
        str(a): ({"NAME": "alpha"}, {"REQUIRES": [], "BUILD_REQUIRES": []}, {}),
        str(b): ({"NAME": "beta"}, {"REQUIRES": ["alpha>=1"], "BUILD_REQUIRES": []}, {}),
    }

    monkeypatch.setattr(app, "_capture_lpmbuild_metadata", lambda script: meta[str(script)])

    args = Namespace(root=str(tmp_path / "root"), source=str(source), output_dir=str(output), dry_run=False, verbose=False)
    chroot_helpers.run_buildgen(args)
    first = (output / "build-manifest.json").read_text(encoding="utf-8")

    chroot_helpers.run_buildgen(args)
    second = (output / "build-manifest.json").read_text(encoding="utf-8")

    assert first == second
    payload = json.loads(first)
    assert [pkg["name"] for pkg in payload["packages"]] == ["alpha", "beta"]


def test_buildgen_cycle_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "packages"
    output = tmp_path / "out"
    a = source / "alpha" / "alpha.lpmbuild"
    b = source / "beta" / "beta.lpmbuild"
    _write_script(a)
    _write_script(b)

    meta = {
        str(a): ({"NAME": "alpha"}, {"REQUIRES": ["beta"], "BUILD_REQUIRES": []}, {}),
        str(b): ({"NAME": "beta"}, {"REQUIRES": ["alpha"], "BUILD_REQUIRES": []}, {}),
    }
    monkeypatch.setattr(app, "_capture_lpmbuild_metadata", lambda script: meta[str(script)])

    args = Namespace(root=str(tmp_path / "root"), source=str(source), output_dir=str(output), dry_run=False, verbose=False)
    with pytest.raises(ValueError, match="Cycle detected in buildgen dependency graph"):
        chroot_helpers.run_buildgen(args)


def test_buildchroot_missing_script(tmp_path: Path) -> None:
    source = tmp_path / "packages"
    out = tmp_path / "out"
    out.mkdir(parents=True)
    (out / "build-manifest.json").write_text(
        json.dumps({"source": str(source), "packages": [{"name": "alpha", "script": "alpha/alpha.lpmbuild"}]}),
        encoding="utf-8",
    )

    args = Namespace(
        root=str(tmp_path / "root"),
        source=str(source),
        cache_dir=str(tmp_path / "cache"),
        output_dir=str(out),
        dry_run=False,
        verbose=False,
    )

    with pytest.raises(ValueError, match=r"Missing \.lpmbuild scripts"):
        chroot_helpers.run_buildchroot(args)
