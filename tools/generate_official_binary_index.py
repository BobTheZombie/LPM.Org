#!/usr/bin/env python3
"""Build packages/index.json for the official LPM binary GitLab repository.

Run in the lpm-org-official-binaries checkout. Do not upload .zst bytes to Git;
Git LFS continues to own those archives. This is publishing tooling, not buildpkg.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=Path, nargs="?", default=Path("."))
    args = parser.parse_args()
    root = args.repo.resolve()
    packages = root / "packages"
    if not packages.is_dir():
        parser.error(f"missing packages directory: {packages}")
    entries = []
    for descriptor in sorted(packages.rglob("*.lpminstall")):
        archive = descriptor.with_suffix(".zst")
        descriptor_sig = Path(str(descriptor) + ".sig")
        archive_sig = Path(str(archive) + ".sig")
        if not archive.is_file() or not descriptor_sig.is_file() or not archive_sig.is_file():
            raise SystemExit(f"missing archive or detached signature: {descriptor}")
        document = json.loads(descriptor.read_text(encoding="utf-8"))
        package = document.get("package", {})
        if document.get("format") != "lpm-install" or document.get("format_version") != 1:
            raise SystemExit(f"invalid descriptor: {descriptor}")
        if package.get("file") != archive.name:
            raise SystemExit(f"archive mismatch in {descriptor}")
        if archive.stat().st_size != int(package.get("size", -1)):
            raise SystemExit(f"size mismatch: {archive}")
        sha = hashlib.sha256()
        with archive.open("rb") as file:
            for block in iter(lambda: file.read(1024 * 1024), b""):
                sha.update(block)
        if sha.hexdigest() != package.get("sha256"):
            raise SystemExit(f"SHA-256 mismatch: {archive}")
        entries.append({
            "descriptor": descriptor.relative_to(root).as_posix(),
            "descriptor_sha256": hashlib.sha256(descriptor.read_bytes()).hexdigest(),
            "data": document,
        })
    output = packages / "index.json"
    output.write_text(json.dumps({
        "format": "lpm-binary-index",
        "version": 1,
        "packages": entries,
    }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {output}: {len(entries)} signed package descriptors")


if __name__ == "__main__":
    main()
