from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence, Tuple

from . import config

logger = logging.getLogger(__name__)


@dataclass
class RepoUpdate:
    repo_dir: Path
    base_url: Optional[str]
    arch: Optional[str] = None


@dataclass
class MaintainerResult:
    artifact_labels: list[str] = field(default_factory=list)
    repo_updates: list[RepoUpdate] = field(default_factory=list)
    staged_paths: list[Path] = field(default_factory=list)
    staged_after_index: list[Path] = field(default_factory=list)


def is_enabled() -> bool:
    return bool(config.DISTRO_MAINTAINER_MODE)


def _ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _copy_with_signature(src: Path, dest_dir: Path) -> list[Path]:
    created: list[Path] = []
    dest_dir = _ensure_directory(dest_dir)
    dest = dest_dir / src.name
    try:
        shutil.copy2(src, dest)
        created.append(dest)
    except Exception as exc:
        logger.warning("Failed to publish package %s -> %s: %s", src, dest, exc)
        return created

    sig_src = src.with_suffix(src.suffix + ".sig")
    if sig_src.exists():
        sig_dest = dest.with_suffix(dest.suffix + ".sig")
        try:
            shutil.copy2(sig_src, sig_dest)
            created.append(sig_dest)
        except Exception as exc:  # pragma: no cover - best effort copy
            logger.warning("Failed to publish signature %s -> %s: %s", sig_src, sig_dest, exc)
    return created


def _repo_base_url(arch: str) -> Optional[str]:
    base = config.DISTRO_REPO_BASE_URL.strip()
    if not base:
        return None
    if base.endswith("/"):
        base = base[:-1]
    return f"{base}/{arch}"


def _collect_artifact_label(meta: object) -> str:
    name = getattr(meta, "name", "unknown")
    version = getattr(meta, "version", "0")
    release = getattr(meta, "release", "1")
    arch = getattr(meta, "arch", "noarch")
    return f"{name}-{version}-{release}.{arch}"


def _archive_sources(source_tree: Optional[Path], meta: object) -> Optional[Path]:
    if not source_tree or not source_tree.exists():
        return None
    try:
        next(source_tree.iterdir())
    except StopIteration:
        return None
    except Exception:
        return None

    target_dir = Path(config.DISTRO_SOURCE_ROOT) / getattr(meta, "name", "package") / getattr(meta, "version", "current")
    _ensure_directory(target_dir)
    archive = target_dir / f"{getattr(meta, 'name', 'package')}-{getattr(meta, 'version', 'current')}-sources.tar.gz"
    try:
        with tarfile.open(archive, "w:gz") as tf:
            for item in sorted(source_tree.rglob("*")):
                try:
                    arcname = item.relative_to(source_tree)
                except ValueError:
                    arcname = item.name
                tf.add(item, arcname=str(arcname))
    except Exception as exc:  # pragma: no cover - fallback logging
        logger.warning("Failed to archive sources from %s: %s", source_tree, exc)
        return None
    return archive


def _write_metadata(
    meta: object,
    script_path: Optional[Path],
    published_artifacts: Iterable[Path],
    source_archive: Optional[Path],
) -> Tuple[Optional[Path], Optional[Path]]:
    target_dir = Path(config.DISTRO_LPMBUILD_ROOT) / getattr(meta, "name", "package") / getattr(meta, "version", "current")
    _ensure_directory(target_dir)

    metadata: dict[str, object] = {
        "name": getattr(meta, "name", ""),
        "version": getattr(meta, "version", ""),
        "release": getattr(meta, "release", ""),
        "arch": getattr(meta, "arch", ""),
        "distribution": config.DISTRO_NAME,
        "generated_at": int(time.time()),
        "artifacts": [str(path) for path in published_artifacts],
    }
    developer = getattr(meta, "developer", "")
    if developer:
        metadata["developer"] = developer
    script_dest_path: Optional[Path] = None
    if script_path and script_path.exists():
        script_dest = target_dir / script_path.name
        try:
            shutil.copy2(script_path, script_dest)
        except Exception as exc:
            logger.warning("Failed to archive lpmbuild %s -> %s: %s", script_path, script_dest, exc)
        else:
            metadata["lpmbuild"] = str(script_dest)
            script_dest_path = script_dest
    if source_archive:
        metadata["source_archive"] = str(source_archive)

    metadata_path = target_dir / "build.json"
    try:
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except Exception as exc:  # pragma: no cover - disk issues
        logger.warning("Failed to write maintainer metadata for %s: %s", getattr(meta, "name", "package"), exc)
        return None, script_dest_path
    return metadata_path, script_dest_path


def handle_lpmbuild(
    *,
    primary: Tuple[object, Path],
    split_records: Sequence[Tuple[Path, object]],
    script_path: Optional[Path],
    source_tree: Optional[Path],
) -> Optional[MaintainerResult]:
    if not is_enabled():
        return None

    meta, package_path = primary
    result = MaintainerResult()

    repo_root = Path(config.DISTRO_REPO_ROOT)
    _ensure_directory(repo_root)

    package_artifacts: list[Path] = []
    arch = getattr(meta, "arch", "noarch") or "noarch"
    repo_dir = repo_root / arch
    package_artifacts.extend(_copy_with_signature(package_path, repo_dir))
    result.staged_paths.extend(package_artifacts)
    result.repo_updates.append(RepoUpdate(repo_dir=repo_dir, base_url=_repo_base_url(arch), arch=arch))
    result.artifact_labels.append(_collect_artifact_label(meta))

    for split_path, split_meta in split_records:
        split_arch = getattr(split_meta, "arch", arch) or arch
        split_repo = repo_root / split_arch
        created = _copy_with_signature(split_path, split_repo)
        package_artifacts.extend(created)
        result.staged_paths.extend(created)
        label = _collect_artifact_label(split_meta)
        result.artifact_labels.append(label)
        update = RepoUpdate(repo_dir=split_repo, base_url=_repo_base_url(split_arch), arch=split_arch)
        if update not in result.repo_updates:
            result.repo_updates.append(update)

    source_archive = _archive_sources(source_tree, meta)
    if source_archive:
        result.staged_paths.append(source_archive)

    metadata_path, script_dest = _write_metadata(
        meta,
        script_path,
        package_artifacts + ([source_archive] if source_archive else []),
        source_archive,
    )
    if metadata_path:
        result.staged_paths.append(metadata_path)
    if script_dest:
        result.staged_paths.append(script_dest)

    return result


def generate_indexes(result: Optional[MaintainerResult], generator) -> None:
    if not result:
        return
    for update in result.repo_updates:
        try:
            generator(update.repo_dir, update.base_url, arch_filter=update.arch)
        except Exception as exc:  # pragma: no cover - propagate message upstream
            logger.warning("Failed to update repository index in %s: %s", update.repo_dir, exc)
            continue
        index_path = update.repo_dir / "index.json"
        result.staged_after_index.append(index_path)


def finalize_git(result: Optional[MaintainerResult]) -> None:
    if not result or not config.DISTRO_GIT_ENABLED:
        return

    git_root = Path(config.DISTRO_GIT_ROOT)
    if not git_root.exists():
        logger.warning("Maintainer git root %s does not exist", git_root)
        return
    staged: list[Path] = []
    for paths in (result.staged_paths, result.staged_after_index):
        for path in paths:
            if path not in staged:
                staged.append(path)
    if not staged:
        return

    add_args = ["git", "-C", str(git_root), "add", "--"]
    for path in staged:
        try:
            rel = path.relative_to(git_root)
            add_args.append(str(rel))
        except ValueError:
            add_args.append(str(path))
    subprocess.run(add_args, check=False)

    status = subprocess.run(
        ["git", "-C", str(git_root), "status", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
    )
    if not status.stdout.strip():
        return

    label = ", ".join(result.artifact_labels) or "packages"
    distro_name = config.DISTRO_NAME or "LPM"
    commit_msg = f"{distro_name}: publish {label}"
    subprocess.run(["git", "-C", str(git_root), "commit", "-m", commit_msg], check=False)

    remote = config.DISTRO_GIT_REMOTE.strip()
    if not remote:
        return
    branch = config.DISTRO_GIT_BRANCH.strip() or "main"
    subprocess.run(["git", "-C", str(git_root), "push", remote, branch], check=False)
