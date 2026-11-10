from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lpm import gitlab_binary_install as transport


def test_raw_gitlab_url_requests_lfs_payload():
    url = transport._file_url(
        "packages/x86_64/konsole/konsole-26.08.2-1.x86_64.zst",
        lfs=True,
    )
    assert "lfs=true" in url
    assert "konsole-26.08.2-1.x86_64.zst" in url
    assert "%2F" in url


def test_reject_path_traversal():
    with pytest.raises(ValueError):
        transport._file_url("packages/../../etc/passwd", lfs=True)


def test_discover_uses_signed_descriptor_metadata_not_filename(monkeypatch, tmp_path):
    archive = "packages/x86_64/konsole/konsole-26.08.2-1.x86_64.zst"
    descriptor = archive.removesuffix(".zst") + ".lpminstall"
    monkeypatch.setattr(transport, "_tree", lambda **_: [
        archive, archive + ".sig", descriptor, descriptor + ".sig"
    ])
    payload = b"package payload"
    sha = hashlib.sha256(payload).hexdigest()
    data = {
        "package": {
            "name": "konsole", "version": "26.08.2", "release": "1",
            "arch": "x86_64", "file": Path(archive).name,
            "sha256": sha, "md5": hashlib.md5(payload).hexdigest(),
            "size": len(payload),
        },
        "metadata": {"requires": ["qt6-base"], "provides": []},
    }

    def fake_stream(url, path, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".lpminstall":
            path.write_text(json.dumps(data))
        else:
            path.write_bytes(b"signed")

    monkeypatch.setattr(transport, "_stream", fake_stream)
    verified = []
    results = transport.discover(
        tmp_path,
        verify_descriptor=lambda desc, sig: verified.append((desc, sig)),
        load_descriptor=lambda path: json.loads(path.read_text()),
    )
    assert len(verified) == 1
    assert len(results) == 1
    candidate, artifact = results[0]
    assert candidate["requires"] == ["qt6-base"]
    assert candidate["blob"] == str(artifact.archive)
    assert artifact.remote_path == archive


def test_materialize_downloads_real_bytes_and_checks_sha(monkeypatch, tmp_path):
    data = b"actual binary payload, not an LFS pointer"
    archive = tmp_path / "archive.zst"
    remote = "packages/x86_64/demo/demo-1-1.x86_64.zst"
    artifact = transport.BinaryArtifact(
        "demo", tmp_path / "desc.lpminstall", archive, remote,
        hashlib.sha256(data).hexdigest(), len(data),
    )
    requested = []

    def fake_stream(url, destination, **kwargs):
        requested.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data if destination == archive else b"signature")

    monkeypatch.setattr(transport, "_stream", fake_stream)
    assert transport.materialize(artifact) == archive
    assert any("lfs=true" in url for url in requested)
    assert Path(str(archive) + ".sig").read_bytes() == b"signature"
    archive.write_bytes(b"corrupted")
    assert transport.materialize(artifact) == archive
    assert archive.read_bytes() == data
