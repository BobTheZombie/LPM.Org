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



def make_dbus_stage(tmp_path: Path, *, mode: int = 0o4750):
    stage = tmp_path / "dbus-stage"
    binary = stage / "usr/libexec/dbus-daemon-launch-helper"
    binary.parent.mkdir(parents=True)
    payload = b"test binary; never executed\n"
    binary.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    binary.chmod(mode)
    return stage, binary, [{
        "path": "/usr/libexec/dbus-daemon-launch-helper",
        "sha256": digest,
        "size": len(payload),
    }]


def test_dbus_meson_upstream_4110_is_normalized_to_4750(tmp_path):
    # Upstream Meson post-install sets setuid + execute-only (04110),
    # while D-Bus documents root:dbus / 04750. Only this exact helper
    # gets normalized; packaged mode and signed metadata remain strict.
    stage, binary, manifest = make_dbus_stage(tmp_path, mode=0o4110)
    annotate_privileged_files(stage, "dbus", manifest)
    assert stat.S_IMODE(binary.lstat().st_mode) == 0o4750
    assert (manifest[0]["mode"], manifest[0]["uid"], manifest[0]["gid"]) == (0o4750, 0, "dbus")
    validate_privileged_files("dbus", manifest, verified=True)


def test_dbus_upstream_mode_not_allowed_for_unrelated_package(tmp_path):
    stage, binary, manifest = make_dbus_stage(tmp_path, mode=0o4110)
    with pytest.raises(ValueError, match="unsupported privileged"):
        annotate_privileged_files(stage, "other-pkg", manifest)
    assert stat.S_IMODE(binary.lstat().st_mode) == 0o4110



def test_dbus_committed_gid_verification_uses_target_root(tmp_path):
    """Guard against converting a signed symbolic group to int('dbus').

    The code path must resolve group IDs against the target root both before
    and after maintainer scripts, not the host's group database.
    """
    import inspect
    from lpm import app as lpm
    from lpm.privileged_files import resolve_privileged_gid

    target = tmp_path / "target"
    (target / "etc").mkdir(parents=True)
    (target / "etc/passwd").write_text(
        "root:x:0:0::/root:/bin/sh\ndbus:x:81:101::/var/lib/dbus:/usr/bin/nologin\n"
    )
    (target / "etc/group").write_text("root:x:0:\nmessagebus:x:101:\n")
    path = "/usr/libexec/dbus-daemon-launch-helper"
    assert resolve_privileged_gid(target, "dbus", path, "dbus") == 101
    assert resolve_privileged_gid(target, "fuse3", "/usr/bin/fusermount3", 0) == 0

    # This internal transaction verifier is nested inside installpkg.
    # Ensure both call sites pass package identity, and symbolic IDs aren't
    # parsed using int() during post-install verification.
    source = inspect.getsource(lpm.installpkg)
    assert 'int(entry["gid"])' not in source
    assert "_verify_committed_payload(mani, meta.name)" in source
    assert "mani, meta.name, verify_content=False" in source
    assert 'root, package_name, str(raw_path), raw_gid' in source
    assert 'elif type(raw_gid) is int' in source


def test_signed_dbus_helper_exact_mode_and_target_group(tmp_path):
    from lpm.privileged_files import resolve_privileged_gid
    stage, binary, manifest = make_dbus_stage(tmp_path)
    annotate_privileged_files(stage, "dbus", manifest)
    assert (manifest[0]["mode"], manifest[0]["uid"], manifest[0]["gid"]) == (0o4750, 0, "dbus")
    validate_privileged_files("dbus", manifest, verified=True)
    target = tmp_path / "target"
    (target / "etc").mkdir(parents=True)
    (target / "etc/group").write_text("root:x:0:\nmessagebus:x:81:\n")
    (target / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/sh\ndbus:x:81:81::/var/lib/dbus:/usr/bin/nologin\n")
    assert resolve_privileged_gid(target, "dbus", manifest[0]["path"], "dbus") == 81



def test_dbus_launch_helper_rejects_extra_primary_group_members(tmp_path):
    from lpm.privileged_files import resolve_privileged_gid
    stage, binary, manifest = make_dbus_stage(tmp_path)
    annotate_privileged_files(stage, "dbus", manifest)
    target = tmp_path / "target"
    (target / "etc").mkdir(parents=True)
    (target / "etc/passwd").write_text("dbus:x:81:81::/var/lib/dbus:/usr/bin/nologin\n")
    (target / "etc/group").write_text("messagebus:x:81:alice,bob\n")
    with pytest.raises(ValueError, match="too many members"):
        resolve_privileged_gid(target, "dbus", manifest[0]["path"], "dbus")


def test_dbus_helper_rejects_unsigned_package(tmp_path):
    stage, binary, manifest = make_dbus_stage(tmp_path)
    annotate_privileged_files(stage, "dbus", manifest)
    with pytest.raises(ValueError, match="signed verification"):
        validate_privileged_files("dbus", manifest, verified=False)


@pytest.mark.parametrize("mode", [0o4755, 0o2750, 0o6750])
def test_dbus_helper_rejects_wrong_privileged_mode(tmp_path, mode):
    stage, binary, manifest = make_dbus_stage(tmp_path, mode=mode)
    with pytest.raises(ValueError, match="unsupported privileged"):
        annotate_privileged_files(stage, "dbus", manifest)


@pytest.mark.parametrize("gid", [0, 1, "root", "wheel"])
def test_dbus_helper_rejects_wrong_group(tmp_path, gid):
    stage, binary, manifest = make_dbus_stage(tmp_path)
    annotate_privileged_files(stage, "dbus", manifest)
    manifest[0]["gid"] = gid
    with pytest.raises(ValueError, match="root:dbus"):
        validate_privileged_files("dbus", manifest, verified=True)


def test_other_package_cannot_stage_privileged_dbus_helper(tmp_path):
    stage, binary, manifest = make_dbus_stage(tmp_path)
    with pytest.raises(ValueError, match="unsupported privileged"):
        annotate_privileged_files(stage, "other-pkg", manifest)


def test_dbus_helper_requires_target_group(tmp_path):
    from lpm.privileged_files import resolve_privileged_gid
    stage, binary, manifest = make_dbus_stage(tmp_path)
    annotate_privileged_files(stage, "dbus", manifest)
    target = tmp_path / "target"
    (target / "etc").mkdir(parents=True)
    (target / "etc/group").write_text("root:x:0:\n")
    (target / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/sh\ndbus:x:81:81::/var/lib/dbus:/usr/bin/nologin\n")
    with pytest.raises(ValueError, match="create the system group"):
        resolve_privileged_gid(target, "dbus", manifest[0]["path"], "dbus")


@pytest.mark.parametrize("injection", [{"uid": 0}, {"gid": 0}, {"mode": 0o755, "uid": 0, "gid": 0}])
def test_unprivileged_file_cannot_smuggle_ownership(tmp_path, injection):
    stage, binary, manifest = make_stage(tmp_path, mode=0o755, name="normal")
    annotate_privileged_files(stage, "ordinary", manifest)
    manifest[0].update(injection)
    with pytest.raises(ValueError, match="ownership metadata"):
        validate_privileged_files("ordinary", manifest, verified=False)



def test_root_install_signed_dbus_helper_restores_target_group(tmp_path, monkeypatch):
    """Exercise signed metadata through the real installer and safe tar filter."""
    import dataclasses
    import os
    import subprocess
    from lpm import app as lpm

    if os.geteuid() != 0:
        pytest.skip("requires an isolated root-owned installation directory")

    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "lpm-dbus-state"))
    stage, binary, manifest = make_dbus_stage(tmp_path, mode=0o4110)
    annotate_privileged_files(stage, "dbus", manifest)
    assert stat.S_IMODE(binary.stat().st_mode) == 0o4750
    meta = lpm.PkgMeta(name="dbus", version="1.16.2", release="4", arch="any")
    (stage / ".lpm-meta.json").write_text(json.dumps(dataclasses.asdict(meta)))
    (stage / ".lpm-manifest.json").write_text(json.dumps(manifest))

    package = tmp_path / "dbus-signed.zst"
    with package.open("wb") as target:
        with lpm.zstd.ZstdCompressor().stream_writer(target, closefd=False) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as tf:
                for file in stage.rglob("*"):
                    if file.is_file():
                        tf.add(file, arcname=file.relative_to(stage).as_posix())

    trust = tmp_path / "keys"
    trust.mkdir()
    private = tmp_path / "private.pem"
    public = trust / "test.pem"
    subprocess.run([
        "openssl", "genpkey", "-algorithm", "RSA",
        "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(private)
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([
        "openssl", "pkey", "-in", str(private),
        "-pubout", "-out", str(public)
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([
        "openssl", "dgst", "-sha256", "-sign", str(private),
        "-out", str(package) + ".sig", str(package)
    ], check=True)
    monkeypatch.setattr(lpm, "TRUST_DIR", trust)

    root = tmp_path / "target"
    (root / "etc").mkdir(parents=True)
    (root / "etc/group").write_text("root:x:0:\nmessagebus:x:81:\n")
    (root / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/sh\ndbus:x:81:81::/var/lib/dbus:/usr/bin/nologin\n")
    lpm.installpkg(package, root=root, verify=True, force=True)
    final = root / "usr/libexec/dbus-daemon-launch-helper"
    assert final.read_bytes() == binary.read_bytes()
    assert stat.S_IMODE(final.stat().st_mode) == 0o4750
    assert (final.stat().st_uid, final.stat().st_gid) == (0, 81)


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


def test_root_install_signed_fuse3_restores_setuid(tmp_path, monkeypatch):
    """Full signed package path under root, including filtered tar extraction."""
    import dataclasses
    import os
    import subprocess
    from lpm import app as lpm

    if os.geteuid() != 0:
        pytest.skip("requires an isolated root-owned installation directory")

    stage, binary, manifest = make_stage(tmp_path)
    annotate_privileged_files(stage, "fuse3", manifest)
    meta = lpm.PkgMeta(name="fuse3", version="3.18.3", release="2", arch="any")
    (stage / ".lpm-meta.json").write_text(json.dumps(dataclasses.asdict(meta)))
    (stage / ".lpm-manifest.json").write_text(json.dumps(manifest))

    package = tmp_path / "fuse3-signed.zst"
    with package.open("wb") as target:
        with lpm.zstd.ZstdCompressor().stream_writer(target, closefd=False) as compressed:
            with tarfile.open(fileobj=compressed, mode="w|") as tf:
                for file in stage.rglob("*"):
                    if file.is_file():
                        tf.add(file, arcname=file.relative_to(stage).as_posix())

    trust = tmp_path / "keys"
    trust.mkdir()
    private = tmp_path / "private.pem"
    public = trust / "test.pem"
    subprocess.run([
        "openssl", "genpkey", "-algorithm", "RSA",
        "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(private)
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([
        "openssl", "pkey", "-in", str(private),
        "-pubout", "-out", str(public)
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run([
        "openssl", "dgst", "-sha256", "-sign", str(private),
        "-out", str(package) + ".sig", str(package)
    ], check=True)
    monkeypatch.setattr(lpm, "TRUST_DIR", trust)

    root = tmp_path / "root"
    root.mkdir()
    lpm.installpkg(package, root=root, verify=True)
    final = root / "usr/bin/fusermount3"
    assert final.read_bytes() == binary.read_bytes()
    assert stat.S_IMODE(final.stat().st_mode) == 0o4755
    assert (final.stat().st_uid, final.stat().st_gid) == (0, 0)
