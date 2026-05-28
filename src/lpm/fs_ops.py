from __future__ import annotations

import json
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Union

from .atomic_io import BytesLike, enforce_umask, read_bytes, safe_write
from .privileges import privileged_section


@contextmanager
def operation_phase(privileged: bool = True):
    """Wrap operations that require deterministic filesystem behaviour."""

    managers = []
    try:
        managers.append(enforce_umask(0o022))
        if privileged:
            managers.append(privileged_section())
        for cm in managers:
            cm.__enter__()
        yield
    finally:
        for cm in reversed(managers):
            cm.__exit__(None, None, None)


def prepare_directory(
    path: Union[str, Path],
    *,
    privileged: bool,
    reset: bool = False,
    fallback_prefix: Optional[str] = None,
) -> Path:
    """Create *path* with deterministic umask and optional reset/fallback behavior.

    When *privileged* is ``True`` this runs in an elevated section and typically
    yields root-owned directories. When ``False`` it runs under current
    privileges and ownership follows the current effective user.
    """

    target = Path(path)
    try:
        with operation_phase(privileged=privileged):
            if reset and target.exists():
                shutil.rmtree(target)
            target.mkdir(parents=True, exist_ok=True)
        return target
    except PermissionError:
        if not fallback_prefix:
            raise
        return Path(tempfile.mkdtemp(prefix=fallback_prefix))


def write_db_json(path: Union[str, Path], obj: Any) -> Path:
    """Atomically write JSON data for package metadata databases."""

    payload = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    return safe_write(path, payload, mode=0o644)


def write_db_bytes(path: Union[str, Path], data: BytesLike) -> Path:
    """Atomically write raw bytes database or index blobs."""

    return safe_write(path, bytes(data), mode=0o644)


def write_manifest_file(
    root: Union[str, Path],
    relpath: Union[str, Path],
    data: Union[str, BytesLike],
    *,
    mode: int,
    owner: Optional[int] = None,
    group: Optional[int] = None,
    is_text: bool = False,
    encoding: str = "utf-8",
) -> Path:
    """Materialize a manifest entry under *root* atomically."""

    root_path = Path(root)
    dest = (root_path / Path(relpath)).resolve()
    payload: Union[str, BytesLike]
    if isinstance(data, str) or is_text:
        payload = data if isinstance(data, str) else bytes(data).decode(encoding)
        return safe_write(dest, payload, mode=mode, owner=owner, group=group, encoding=encoding)
    payload = bytes(data)
    return safe_write(dest, payload, mode=mode, owner=owner, group=group)


def journal_append(journal_path: Union[str, Path], entry: Dict[str, Any]) -> Path:
    """Append a JSON entry to the journal atomically."""

    path = Path(journal_path)
    try:
        existing = read_bytes(path)
    except FileNotFoundError:
        existing = b""
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
    return safe_write(path, existing + line.encode("utf-8"), mode=0o640)


def materialize_from_manifest(
    root: Union[str, Path],
    items: Iterable[Dict[str, Any]],
    *,
    owner: Optional[int] = None,
    group: Optional[int] = None,
) -> None:
    """Write multiple manifest entries to *root* atomically."""

    for item in items:
        rel = item["path"]
        mode = item["mode"]
        if "data" in item:
            write_manifest_file(root, rel, item["data"], mode=mode, owner=owner, group=group)
        else:
            text = item.get("text", "")
            write_manifest_file(
                root,
                rel,
                text,
                mode=mode,
                owner=owner,
                group=group,
                is_text=True,
            )


__all__ = [
    "operation_phase",
    "prepare_directory",
    "write_db_json",
    "write_db_bytes",
    "write_manifest_file",
    "journal_append",
    "materialize_from_manifest",
]
