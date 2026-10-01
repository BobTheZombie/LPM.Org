"""Signed distribution metadata for installing LPM binary packages."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any, Mapping, Optional

FORMAT = "lpm-install"
FORMAT_VERSION = 1


def file_digest(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with Path(path).open("rb", buffering=1024 * 1024) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def md5sum(path: Path) -> str:
    """Return MD5 for compatibility metadata, never as the trust primitive."""

    return file_digest(path, "md5")


def sha256sum(path: Path) -> str:
    return file_digest(path, "sha256")


def artifact_location(package: Path, base_url: Optional[str]) -> str:
    if base_url:
        return base_url.rstrip("/") + "/" + package.name
    return package.name


def descriptor_path(package: Path) -> Path:
    package = Path(package)
    return package.with_name(package.name.removesuffix(".zst") + ".lpminstall")


def build_descriptor(
    package: Path,
    metadata: Mapping[str, Any],
    *,
    base_url: Optional[str] = None,
) -> dict[str, Any]:
    package = Path(package)
    package_url = artifact_location(package, base_url)
    signature_name = package.name + ".sig"
    signature_url = artifact_location(package.with_name(signature_name), base_url)
    list_fields = {
        "requires", "build_requires", "provides", "conflicts", "obsoletes",
        "recommends", "suggests", "deltas",
    }
    metadata_fields = (
        "summary", "url", "license", "developer", "requires",
        "build_requires", "provides", "provides_by_package", "conflicts",
        "obsoletes", "recommends", "suggests", "kernel",
        "mkinitcpio_preset", "deltas",
    )
    metadata_payload: dict[str, Any] = {}
    for key in metadata_fields:
        if key in list_fields:
            default: Any = []
        elif key == "provides_by_package":
            default = {}
        elif key == "kernel":
            default = False
        elif key == "mkinitcpio_preset":
            default = None
        else:
            default = ""
        metadata_payload[key] = metadata.get(key, default)
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "generated": int(time.time()),
        "package": {
            "name": str(metadata["name"]),
            "version": str(metadata["version"]),
            "release": str(metadata.get("release", "1")),
            "arch": str(metadata.get("arch", "noarch")),
            "file": package.name,
            "url": package_url,
            "size": package.stat().st_size,
            "md5": md5sum(package),
            "sha256": sha256sum(package),
            "signature": {
                "algorithm": "openssl-dgst-sha256",
                "file": signature_name,
                "url": signature_url,
            },
        },
        "metadata": metadata_payload,
    }


def write_descriptor(
    package: Path,
    metadata: Mapping[str, Any],
    *,
    base_url: Optional[str] = None,
) -> Path:
    output = descriptor_path(package)
    payload = build_descriptor(package, metadata, base_url=base_url)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, output)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return output


def sign_file(path: Path, key: Path) -> Path:
    signature = Path(str(path) + ".sig")
    signature.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{signature.name}.", dir=signature.parent)
    os.close(fd)
    try:
        subprocess.run(
            [
                "openssl", "dgst", "-sha256", "-sign", str(key),
                "-out", tmp_name, str(path),
            ],
            check=True,
        )
        os.replace(tmp_name, signature)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return signature


def load_descriptor(path: Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as stream:
        data = json.load(stream)
    if data.get("format") != FORMAT or data.get("format_version") != FORMAT_VERSION:
        raise ValueError("unsupported .lpminstall format")
    package = data.get("package")
    if not isinstance(package, dict):
        raise ValueError(".lpminstall package section is missing")
    for key in ("name", "version", "release", "arch", "url", "size", "md5", "sha256"):
        if package.get(key) in (None, ""):
            raise ValueError(f".lpminstall package.{key} is missing")
    if len(str(package["md5"])) != 32 or len(str(package["sha256"])) != 64:
        raise ValueError(".lpminstall contains an invalid checksum")
    return data


def resolve_location(descriptor: Path, location: str) -> str:
    parsed = urllib.parse.urlparse(location)
    if parsed.scheme in {"http", "https", "file"} or location.startswith("/"):
        return location
    return str((Path(descriptor).parent / location).resolve())


__all__ = [
    "FORMAT", "FORMAT_VERSION", "artifact_location", "build_descriptor",
    "descriptor_path", "file_digest", "load_descriptor", "md5sum",
    "resolve_location", "sha256sum", "sign_file", "write_descriptor",
]
