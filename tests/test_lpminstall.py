import json
from pathlib import Path

import pytest

from src.lpm.lpminstall import (
    load_descriptor,
    md5sum,
    resolve_location,
    sha256sum,
    write_descriptor,
)


def _metadata():
    return {
        "name": "demo",
        "version": "2.0",
        "release": "3",
        "arch": "x86_64",
        "summary": "Demo package",
        "requires": ["glibc >= 2.42"],
        "provides": ["demo-api=2"],
        "conflicts": [],
        "obsoletes": [],
        "recommends": ["demo-docs"],
        "suggests": [],
        "deltas": [],
    }


def test_descriptor_contains_binary_integrity_and_dependencies(tmp_path: Path):
    package = tmp_path / "demo-2.0-3.x86_64.zst"
    package.write_bytes(b"\x28\xb5\x2f\xfdpackage payload")

    spec_path = write_descriptor(package, _metadata())
    spec = load_descriptor(spec_path)

    assert spec_path.name == "demo-2.0-3.x86_64.lpminstall"
    assert spec["package"]["md5"] == md5sum(package)
    assert spec["package"]["sha256"] == sha256sum(package)
    assert spec["package"]["size"] == package.stat().st_size
    assert spec["metadata"]["requires"] == ["glibc >= 2.42"]
    assert resolve_location(spec_path, spec["package"]["url"]) == str(package)


def test_descriptor_uses_distribution_urls(tmp_path: Path):
    package = tmp_path / "demo.zst"
    package.write_bytes(b"package")
    spec = load_descriptor(
        write_descriptor(package, _metadata(), base_url="https://repo.example/core")
    )
    assert spec["package"]["url"] == "https://repo.example/core/demo.zst"
    assert spec["package"]["signature"]["url"] == "https://repo.example/core/demo.zst.sig"


def test_invalid_descriptor_is_rejected(tmp_path: Path):
    spec = tmp_path / "broken.lpminstall"
    spec.write_text(json.dumps({"format": "lpm-install", "format_version": 1}))
    with pytest.raises(ValueError):
        load_descriptor(spec)
