"""Install-only transport for LPM's official GitLab LFS binary repository.

This module deliberately does not participate in buildpkg or source recipes.
GitLab's raw-file API with lfs=true streams actual LFS content rather than
Git pointer text. All downloaded artifacts are verified against the signed
.lpminstall descriptor before the standard LPM installer handles them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

PROJECT = "lpm-org/lpm-org-official-binaries"
DEFAULT_REF = "main"
API = "https://gitlab.com/api/v4/projects/" + urllib.parse.quote(PROJECT, safe="")
VALID_SHA = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class BinaryArtifact:
    name: str
    descriptor: Path
    archive: Path
    remote_path: str
    expected_sha256: str
    expected_size: int


def _headers() -> dict[str, str]:
    result = {"User-Agent": "LPM-Linux-binary-install/1", "Accept": "application/json"}
    token = os.environ.get("LPM_GITLAB_TOKEN", "")
    if token:
        result["PRIVATE-TOKEN"] = token
    return result


def _request(url: str):
    return urllib.request.urlopen(urllib.request.Request(url, headers=_headers()), timeout=45)


def _json(url: str):
    with _request(url) as response:
        return json.load(response)


def _file_url(path: str, *, lfs: bool = False, ref: str = DEFAULT_REF) -> str:
    if not path.startswith("packages/") or ".." in Path(path).parts:
        raise ValueError("invalid package path in GitLab repository")
    encoded = urllib.parse.quote(path, safe="")
    return f"{API}/repository/files/{encoded}/raw?" + urllib.parse.urlencode(
        {"ref": ref, "lfs": "true" if lfs else "false"}
    )


def _stream(url: str, destination: Path, *, maximum_bytes: int | None = None) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".lpm-download-", dir=destination.parent)
    written = 0
    try:
        with os.fdopen(fd, "wb") as output, _request(url) as response:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                written += len(block)
                if maximum_bytes is not None and written > maximum_bytes:
                    raise RuntimeError("GitLab response exceeded expected size")
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _tree(*, ref: str = DEFAULT_REF) -> list[str]:
    paths = []
    page = 1
    while True:
        url = f"{API}/repository/tree?" + urllib.parse.urlencode({
            "path": "packages", "recursive": "true",
            "ref": ref, "per_page": 100, "page": page,
        })
        with _request(url) as response:
            entries = json.load(response)
            next_page = response.headers.get("X-Next-Page", "")
        if not isinstance(entries, list):
            raise RuntimeError("GitLab returned an invalid repository tree")
        paths.extend(e["path"] for e in entries
                     if e.get("type") == "blob" and isinstance(e.get("path"), str))
        if not next_page:
            break
        page = int(next_page)
    return paths


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def _cache_file(cache: Path, remote: str) -> Path:
    # Cache paths mirror the official repo layout, preventing name collisions.
    return cache / remote


def discover(
    cache: Path,
    *,
    verify_descriptor: Callable[[Path, Path], None],
    load_descriptor: Callable[[Path], Mapping],
    verify: bool = True,
    ref: str = DEFAULT_REF,
) -> list[tuple[dict, BinaryArtifact]]:
    """Return signed metadata for all published packages without fetching archives."""
    paths = set(_tree(ref=ref))
    result = []
    for remote in sorted(p for p in paths if p.endswith(".lpminstall")):
        archive_remote = remote.removesuffix(".lpminstall") + ".zst"
        signed_remote = remote + ".sig"
        if archive_remote not in paths:
            continue
        if verify and signed_remote not in paths:
            raise RuntimeError(f"unsigned descriptor in official repo: {remote}")
        descriptor = _cache_file(cache, remote)
        signature = _cache_file(cache, signed_remote)
        # Refresh metadata on every discovery: a removed/revoked signature must
        # never be silently replaced by a cached old signature.
        _stream(_file_url(remote, ref=ref), descriptor, maximum_bytes=4 * 1024 * 1024)
        if verify:
            _stream(_file_url(signed_remote, ref=ref), signature, maximum_bytes=256 * 1024)
            verify_descriptor(descriptor, signature)
        data = load_descriptor(descriptor)
        pkg = data["package"]
        metadata = data.get("metadata") or {}
        if pkg.get("file") != Path(archive_remote).name:
            raise RuntimeError(f"descriptor archive mismatch: {remote}")
        if not VALID_SHA.fullmatch(str(pkg["sha256"])):
            raise RuntimeError(f"invalid package SHA-256: {remote}")
        size = int(pkg["size"])
        if size <= 0:
            raise RuntimeError(f"invalid package size: {remote}")
        if archive_remote + ".sig" not in paths and verify:
            raise RuntimeError(f"missing package signature: {archive_remote}")
        raw = dict(metadata)
        raw.update({
            "name": pkg["name"], "version": pkg["version"],
            "release": pkg["release"], "arch": pkg["arch"],
            "size": size, "md5": pkg["md5"], "sha256": pkg["sha256"],
            "blob": str(_cache_file(cache, archive_remote)),
            "lpminstall": str(descriptor),
            "deltas": [],
        })
        result.append((raw, BinaryArtifact(
            name=str(pkg["name"]), descriptor=descriptor,
            archive=_cache_file(cache, archive_remote),
            remote_path=archive_remote,
            expected_sha256=str(pkg["sha256"]), expected_size=size,
        )))
    return result


def materialize(
    artifact: BinaryArtifact, *, verify: bool = True, ref: str = DEFAULT_REF,
) -> Path:
    """Stream selected .zst and detached signature, then verify exact bytes."""
    path = artifact.archive
    if path.is_file():
        if path.stat().st_size != artifact.expected_size or _sha256(path) != artifact.expected_sha256:
            path.unlink()
    if not path.is_file():
        _stream(_file_url(artifact.remote_path, lfs=True, ref=ref),
                path, maximum_bytes=artifact.expected_size)
    if path.stat().st_size != artifact.expected_size or _sha256(path) != artifact.expected_sha256:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"GitLab artifact integrity failure: {artifact.remote_path}")
    if verify:
        # Always refresh signature; otherwise stale signature may be accepted.
        _stream(_file_url(artifact.remote_path + ".sig", ref=ref),
                Path(str(path) + ".sig"), maximum_bytes=256 * 1024)
    return path
