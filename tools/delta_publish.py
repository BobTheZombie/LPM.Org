#!/usr/bin/env python3
"""Generate zstd-based delta patches for repository packages."""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import ZSTD_MIN_VERSION, load_conf
from src.lpm.delta import DeltaMeta, delta_relpath, generate_delta


def _package_version_label(pkg: Dict[str, Any]) -> str:
    version = str(pkg.get("version", ""))
    release = str(pkg.get("release", "1")) or "1"
    return f"{version}-{release}" if release else version


def _artifact_name(blob: str) -> str:
    parsed = urllib.parse.urlparse(blob)
    path = parsed.path or blob
    return Path(path).name


def _artifact_path(repo_root: Path, pkg: Dict[str, Any]) -> Optional[Path]:
    blob = pkg.get("blob")
    if not blob:
        return None
    return repo_root / _artifact_name(str(blob))


def _select_previous(packages: List[Dict[str, Any]], pkg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = pkg.get("name")
    arch = pkg.get("arch")
    version = str(pkg.get("version", ""))
    release = str(pkg.get("release", "1"))

    candidates = [
        entry
        for entry in packages
        if entry.get("name") == name and entry.get("arch") == arch
    ]
    if not candidates:
        return None

    def _key(entry: Dict[str, Any]) -> tuple[str, str]:
        return str(entry.get("version", "")), str(entry.get("release", "1"))

    candidates.sort(key=_key)
    previous: Optional[Dict[str, Any]] = None
    for entry in candidates:
        if str(entry.get("version", "")) == version and str(entry.get("release", "1")) == release:
            return previous
        previous = entry
    return previous


def _load_min_version(config_path: Path) -> str:
    conf = load_conf(config_path)
    return conf.get("ZSTD_MIN_VERSION", ZSTD_MIN_VERSION)


def _update_delta_metadata(pkg: Dict[str, Any], rel_url: str, meta: DeltaMeta, base_version: str) -> None:
    entry = {
        "algorithm": meta.algorithm,
        "base_version": base_version,
        "base_sha256": meta.base_sha256,
        "url": rel_url,
        "sha256": meta.delta_sha256,
        "size": meta.delta_size,
        "min_tool": meta.min_tool,
    }
    deltas = pkg.setdefault("deltas", [])
    pkg["deltas"] = [d for d in deltas if d.get("url") != rel_url]
    pkg["deltas"].append(entry)


def generate_deltas(repo_root: Path, index_path: Path, config_path: Path) -> bool:
    min_version = _load_min_version(config_path)
    index = json.loads(index_path.read_text(encoding="utf-8"))
    packages: List[Dict[str, Any]] = index.get("packages", [])
    changed = False

    for pkg in packages:
        target_path = _artifact_path(repo_root, pkg)
        if not target_path or not target_path.exists():
            continue

        previous = _select_previous(packages, pkg)
        if not previous:
            continue

        base_path = _artifact_path(repo_root, previous)
        if not base_path or not base_path.exists():
            continue

        base_version = _package_version_label(previous)
        target_version = _package_version_label(pkg)
        rel_path = delta_relpath(pkg["name"], target_version, pkg.get("arch", "noarch"), base_version)
        out_path = repo_root / rel_path

        meta = generate_delta(base_path, target_path, out_path, min_version)
        if not meta:
            continue

        meta.base_version = base_version
        rel_url = str(rel_path).replace("\\", "/")
        _update_delta_metadata(pkg, rel_url, meta, base_version)
        print(f"generated delta {rel_url} for {pkg['name']} {target_version}")
        changed = True

    if changed:
        index_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True, help="repository root containing packages")
    parser.add_argument(
        "--index",
        type=Path,
        required=True,
        help="path to index.json to update",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("/etc/lpm/lpm.conf"),
        help="configuration file used for defaults",
    )
    args = parser.parse_args()

    try:
        changed = generate_deltas(args.repo_root, args.index, args.config)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"delta generation failed: {exc}", file=sys.stderr)
        return 1

    if changed:
        print("delta index updated")
    else:
        print("no deltas generated (nothing to do)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
