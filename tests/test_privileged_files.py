"""Security regression tests for LPM's allowlisted privileged file modes."""
from __future__ import annotations

import hashlib
import io
import json
import stat
import tarfile
from pathlib import Path

import pytest

from lpm.privileged_files import (
    PRIVILEGED_FILES,
    annotate_privileged_files,
    validate_privileged_files,
)


def make_stage(tmp_path: Path, *, name: str = "fusermount3", mode: int = 0o4755):
    root = tmp_path / "stage"
    binary = root / "usr" / "bin" / name
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"ELF-style test payload only; never executed\n")
    binary.chmod(mode)
    entry = {
        "path": "/usr/bin/" + name,
        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "size": binary.stat().st_size,
    }
    return root, binary, [entry]


def test_allowlisted_fuse3_manifest_is_root_owned_and_exact_mode(tmp_path):
    stage, binary, manifest = make_stage(tmp_path)
    annotate_privileged_files(stage, "fuse3", manifest)
    assert manifest[0]["mode"] == 0o4755
    assert manifest[0]["uid"] == manifest[0]["gid"] == 0
    validate_privileged_files("fuse3", manifest, verified=True)


def test_unsigned_fuse3_package_does_not_gain_privileges(tmp_path):
    stage, binary, manifest = make_stage(tmp_path)
    annotate_privileged_files(stage, "fuse3", manifest)
    with pytest.raises(ValueError, match="signed verification"):
        validate_privileged_files("fuse3", manifest, verified=False)


def test_other_package_cannot_stage_setuid_fusermount3(tmp_path):
    stage, binary, manifest = make_stage(tmp_path)
    with pytest.raises(ValueError, match="unsupported privileged"):
        annotate_privileged_files(stage, "other-pkg", manifest)


def test_setgid_or_unexpected_mode_not_allowed(tmp_path):
    stage, binary, manifest = make_stage(tmp_path, mode=0o2755)
    with pytest.raises(ValueError, match="unsupported privileged"):
        annotate_privileged_files(stage, "fuse3", manifest)


def test_missing_setuid_on_fusermount3_fails_build(tmp_path):
    stage, binary, manifest = make_stage(tmp_path, mode=0o755)
    with pytest.raises(ValueError, match="expected a regular"):
        annotate_privileged_files(stage, "fuse3", manifest)


@pytest.mark.parametrize("change", [
    {"mode": 0o6755},
    {"uid": 1000},
    {"gid": 1000},
    {"link": "evil"},
    {"sha256": "NOT_A_HASH"},
    {"path": "/usr/bin/../bin/fusermount3"},
])
def test_tampered_signed_privileged_manifest_rejected(tmp_path, change):
    stage, binary, manifest = make_stage(tmp_path)
    annotate_privileged_files(stage, "fuse3", manifest)
    manifest[0].update(change)
    with pytest.raises(ValueError):
        validate_privileged_files("fuse3", manifest, verified=True)


def test_duplicate_privileged_manifest_entries_rejected(tmp_path):
    stage, binary, manifest = make_stage(tmp_path)
    annotate_privileged_files(stage, "fuse3", manifest)
    with pytest.raises(ValueError, match="duplicated"):
        validate_privileged_files("fuse3", [manifest[0], dict(manifest[0])], verified=True)


def test_unprivileged_manifest_is_not_rejected(tmp_path):
    stage, binary, manifest = make_stage(tmp_path, mode=0o755, name="normal")
    annotate_privileged_files(stage, "ordinary", manifest)
    validate_privileged_files("ordinary", manifest, verified=False)
    assert "mode" not in manifest[0]


def test_python_data_filter_stays_enabled_and_avoids_archived_setuid(tmp_path):
    src = tmp_path / "source"
    src.write_bytes(b"safe test")
    src.chmod(0o4755)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as tf:
        tf.add(src, arcname="usr/bin/fusermount3")
    stream.seek(0)
    extracted = tmp_path / "extract"
    extracted.mkdir()
    with tarfile.open(fileobj=stream, mode="r") as tf:
        for member in tf:
            tf.extract(member, path=str(extracted), filter="data")
    resulting = extracted / "usr/bin/fusermount3"
    assert stat.S_IMODE(resulting.stat().st_mode) & stat.S_ISUID == 0


def test_installer_rejects_unverified_privileged_package(tmp_path, monkeypatch):
    # Verify the real installer entry point rejects a crafted privileged
    # manifest before any payload can be installed.
    import dataclasses
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "lpm-state"))
    from lpm import app as lpm

    stage, binary, manifest = make_stage(tmp_path)
    annotate_privileged_files(stage, "fuse3", manifest)
    meta = lpm.PkgMeta(name="fuse3", version="3.18.3", release="2", arch="any")
    (stage / ".lpm-meta.json").write_text(json.dumps(dataclasses.asdict(meta)))
    (stage / ".lpm-manifest.json").write_text(json.dumps(manifest))
    package = tmp_path / "fuse3.zst"
    with package.open("wb") as target:
        compressor = lpm.zstd.ZstdCompressor()
        with compressor.stream_writer(target, closefd=False) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as tf:
                for file in stage.rglob("*"):
                    if file.is_file():
                        tf.add(file, arcname=file.relative_to(stage).as_posix())

    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(SystemExit):
        lpm.installpkg(package, root=root, verify=False)
    assert not (root / "usr/bin/fusermount3").exists()
