from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lpm import gitlab_binary_install as transport


def _catalog(archive_bytes=b"payload"):
    archive_path = "packages/x86_64/demo/demo-1.0-1.x86_64.zst"
    descriptor_path = archive_path.removesuffix(".zst") + ".lpminstall"
    document = {
        "format": "lpm-install", "format_version": 1,
        "package": {
            "name": "demo", "version": "1.0", "release": "1",
            "arch": "x86_64", "file": Path(archive_path).name,
            "size": len(archive_bytes),
            "md5": hashlib.md5(archive_bytes).hexdigest(),
            "sha256": hashlib.sha256(archive_bytes).hexdigest(),
        }, "metadata": {"requires": ["qt6-base"], "provides": ["demo-virtual"]},
    }
    raw = json.dumps(document).encode()
    catalog = {"format": "lpm-binary-index", "version": 1, "packages": [{
        "descriptor": descriptor_path, "descriptor_sha256": hashlib.sha256(raw).hexdigest(),
        "data": document,
    }]}
    return catalog, descriptor_path, archive_path, raw, archive_bytes


def test_lfs_raw_url():
    url = transport._file_url("packages/x86_64/demo/demo-1.0-1.x86_64.zst", lfs=True)
    assert "lfs=true" in url
    assert "%2F" in url


def test_unsafe_path_rejected():
    with pytest.raises(ValueError):
        transport._file_url("packages/../../etc/passwd", lfs=True)


def test_discovery_fetches_one_index_and_no_descriptors(monkeypatch, tmp_path):
    catalog, desc, archive, raw, payload = _catalog()
    urls = []

    def download(url, target, **kwargs):
        urls.append(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(catalog))

    monkeypatch.setattr(transport, "_stream", download)
    results = transport.discover(tmp_path)
    assert len(urls) == 1
    assert "index.json" in urls[0]
    candidate, artifact = results[0]
    assert candidate["name"] == "demo"
    assert candidate["requires"] == ["qt6-base"]
    assert artifact.descriptor_remote == desc
    assert artifact.archive_remote == archive


def test_selected_package_requires_signed_descriptor_and_valid_payload(monkeypatch, tmp_path):
    catalog, desc, archive, raw, payload = _catalog()
    artifact = transport._descriptor_to_candidate(catalog["packages"][0]["data"], desc, tmp_path)[1]
    from dataclasses import replace
    artifact = replace(artifact, descriptor_sha256=hashlib.sha256(raw).hexdigest())
    downloads = []

    def download(url, target, **kwargs):
        downloads.append(url)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target == artifact.descriptor:
            target.write_bytes(raw)
        elif target == artifact.archive:
            target.write_bytes(payload)
        else:
            target.write_bytes(b"signature")

    monkeypatch.setattr(transport, "_stream", download)
    called = []
    result = transport.materialize(
        artifact, verify_descriptor=lambda a, b: called.append(("descriptor", a, b)),
        load_descriptor=lambda p: json.loads(p.read_text()),
        verify_package=lambda a, b: called.append(("package", a, b)),
    )
    assert result.read_bytes() == payload
    assert any("lfs=true" in url for url in downloads)
    assert [x[0] for x in called] == ["descriptor", "package"]


def test_index_does_not_override_signed_metadata(monkeypatch, tmp_path):
    catalog, desc, archive, raw, payload = _catalog()
    fake = dict(catalog)
    fake["packages"] = [dict(catalog["packages"][0])]
    fake["packages"][0]["data"] = json.loads(json.dumps(catalog["packages"][0]["data"]))
    fake["packages"][0]["data"]["metadata"]["requires"] = ["evil-dependency"]
    # A tampered index cannot change signed metadata without changing the
    # expected descriptor digest; materialize will reject it.
    fake_doc = json.dumps(fake["packages"][0]["data"]).encode()
    artifact = transport._descriptor_to_candidate(fake["packages"][0]["data"], desc, tmp_path)[1]
    from dataclasses import replace
    artifact = replace(artifact, descriptor_sha256=hashlib.sha256(fake_doc).hexdigest())

    def download(url, target, **kwargs):
        target.parent.mkdir(parents=True, exist_ok=True)
        if target == artifact.descriptor:
            target.write_bytes(raw)
        else:
            target.write_bytes(b"signature")

    monkeypatch.setattr(transport, "_stream", download)
    with pytest.raises(RuntimeError, match="index and signed descriptor disagree"):
        transport.materialize(
            artifact, verify_descriptor=lambda a, b: None,
            load_descriptor=lambda p: json.loads(p.read_text()),
            verify_package=lambda a, b: None,
        )
