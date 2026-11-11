"""Install-only access to LPM's official GitLab LFS binary repository.

Discovery requires ONE small index request; no bulk per-package API reads.
The index is only an untrusted catalog. Each selected package's signed
.lpminstall is verified against trusted LPM public keys before any install.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import os
import re
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

PROJECT = "lpm-org/lpm-org-official-binaries"
REF = "main"
API = "https://gitlab.com/api/v4/projects/" + urllib.parse.quote(PROJECT, safe="")
HEX_SHA = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class BinaryArtifact:
    name: str
    descriptor: Path
    archive: Path
    descriptor_remote: str
    archive_remote: str
    expected_sha256: str
    expected_size: int
    descriptor_sha256: str


def _headers() -> dict[str, str]:
    result = {"User-Agent": "LPM-Aurora-install/1"}
    if os.getenv("LPM_GITLAB_TOKEN"):
        result["PRIVATE-TOKEN"] = os.environ["LPM_GITLAB_TOKEN"]
    return result


def _file_url(path: str, *, lfs: bool = False) -> str:
    parts = Path(path).parts
    if not parts or parts[0] != "packages" or any(p in {"..", ".", ""} for p in parts):
        raise ValueError(f"invalid official binary path: {path}")
    encoded = urllib.parse.quote(path, safe="")
    return f"{API}/repository/files/{encoded}/raw?" + urllib.parse.urlencode(
        {"ref": REF, "lfs": "true" if lfs else "false"}
    )


def _stream(url: str, destination: Path, *, max_size: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".lpm-download-", dir=destination.parent)
    received = 0
    try:
        request = urllib.request.Request(url, headers=_headers())
        with os.fdopen(fd, "wb") as sink, urllib.request.urlopen(request, timeout=60) as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > max_size:
                    raise RuntimeError("official repository response exceeds size limit")
                sink.write(chunk)
            sink.flush()
            os.fsync(sink.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def _descriptor_to_candidate(data: Mapping, descriptor_remote: str, cache: Path):
    package = data.get("package")
    if data.get("format") != "lpm-install" or data.get("format_version") != 1:
        raise ValueError(f"unsupported metadata in {descriptor_remote}")
    if not isinstance(package, dict):
        raise ValueError(f"missing package fields: {descriptor_remote}")
    archive_remote = descriptor_remote.removesuffix(".lpminstall") + ".zst"
    filename = Path(archive_remote).name
    if package.get("file") != filename:
        raise ValueError(f"archive name mismatch: {descriptor_remote}")
    if not HEX_SHA.fullmatch(str(package.get("sha256", ""))):
        raise ValueError(f"invalid SHA-256 in {descriptor_remote}")
    size = int(package["size"])
    if size <= 0:
        raise ValueError(f"invalid size in {descriptor_remote}")
    for key in ("name", "version", "release", "arch", "md5"):
        if not isinstance(package.get(key), str) or not package[key]:
            raise ValueError(f"missing {key} in {descriptor_remote}")
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("invalid metadata")
    item = dict(metadata)
    item.update({
        "name": package["name"], "version": package["version"],
        "release": package["release"], "arch": package["arch"],
        "size": size, "sha256": package["sha256"], "md5": package["md5"],
        "blob": str(cache / archive_remote), "lpminstall": str(cache / descriptor_remote),
        "deltas": [],
    })
    artifact = BinaryArtifact(
        package["name"], cache / descriptor_remote, cache / archive_remote,
        descriptor_remote, archive_remote, package["sha256"], size, "",
    )
    return item, artifact


def discover(cache: Path) -> list[tuple[dict, BinaryArtifact]]:
    """Read the aggregated catalog once; it is untrusted until selected descriptors are signed."""
    index_path = cache / "packages" / "index.json"
    # A throttled refresh must not discard previously validated catalog bytes.
    # Use a recent cache only on HTTP 429; never hide malformed/404 responses.
    try:
        _stream(_file_url("packages/index.json"), index_path, max_size=16 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        if exc.code != 429:
            raise
        max_age = 24 * 60 * 60
        age = time.time() - index_path.stat().st_mtime if index_path.is_file() else float("inf")
        if age > max_age:
            retry = exc.headers.get("Retry-After", "unspecified")
            raise RuntimeError(
                f"GitLab rate limited the repository index (HTTP 429; Retry-After: {retry}). "
                "No usable cached index is available. Avoid rapid retries."
            ) from exc
        import warnings
        warnings.warn(
            f"GitLab HTTP 429: using cached Aurora binary index ({int(age)} seconds old)",
            RuntimeWarning,
            stacklevel=2,
        )
    data = json.loads(index_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("format") != "lpm-binary-index" or data.get("version") != 1:
        raise ValueError("unsupported GitLab binary index")
    packages = data.get("packages", [])
    if not isinstance(packages, list) or len(packages) > 50000:
        raise ValueError("invalid GitLab binary index packages")
    entries = []
    for entry in packages:
        if not isinstance(entry, dict):
            raise ValueError("invalid index entry")
        path = entry.get("descriptor")
        if not isinstance(path, str) or not path.endswith(".lpminstall"):
            raise ValueError("invalid descriptor path in index")
        # Validate paths before joining them under the cache.
        _file_url(path)
        checksum = entry.get("descriptor_sha256", "")
        if not isinstance(checksum, str) or not HEX_SHA.fullmatch(checksum):
            raise ValueError(f"missing descriptor SHA-256: {path}")
        candidate, artifact = _descriptor_to_candidate(entry.get("data", {}), path, cache)
        from dataclasses import replace
        entries.append((candidate, replace(artifact, descriptor_sha256=checksum)))
    return entries


def materialize(
    artifact: BinaryArtifact,
    *,
    verify_descriptor: Callable[[Path, Path], None],
    load_descriptor: Callable[[Path], Mapping],
    verify_package: Callable[[Path, Path], None],
) -> Path:
    """Verify signed descriptor and archive; do not trust metadata from index."""
    desc = artifact.descriptor
    sig = Path(str(desc) + ".sig")
    _stream(_file_url(artifact.descriptor_remote), desc, max_size=4 * 1024 * 1024)
    _stream(_file_url(artifact.descriptor_remote + ".sig"), sig, max_size=256 * 1024)
    verify_descriptor(desc, sig)
    if _digest(desc) != artifact.descriptor_sha256:
        raise RuntimeError(f"index and signed descriptor disagree: {artifact.name}")
    signed_package = load_descriptor(desc)["package"]
    if any(signed_package[key] != expected for key, expected in (
        ("name", artifact.name), ("sha256", artifact.expected_sha256),
        ("size", artifact.expected_size),
    )):
        raise RuntimeError(f"signed descriptor mismatch: {artifact.name}")

    archive = artifact.archive
    if archive.exists() and (archive.stat().st_size != artifact.expected_size or
                             _digest(archive) != artifact.expected_sha256):
        archive.unlink()
    if not archive.exists():
        _stream(_file_url(artifact.archive_remote, lfs=True), archive,
                max_size=artifact.expected_size)
    if archive.stat().st_size != artifact.expected_size or _digest(archive) != artifact.expected_sha256:
        archive.unlink(missing_ok=True)
        raise RuntimeError(f"archive integrity mismatch: {artifact.name}")
    archive_sig = Path(str(archive) + ".sig")
    _stream(_file_url(artifact.archive_remote + ".sig"), archive_sig,
            max_size=256 * 1024)
    verify_package(archive, archive_sig)
    return archive
