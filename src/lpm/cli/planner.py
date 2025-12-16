"""Helpers for constructing installation plans."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .. import app as _app


def _normalize_path(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.expanduser()


def build_install_plan(
    packages: Iterable[str],
    *,
    root: str | Path | None = None,
    verify: bool = True,
    force: bool = False,
    allow_fallback: bool | None = None,
) -> dict[str, Any]:
    """Return the plan dictionary that will be executed as ``root``.

    Each package entry records the path to the package payload and signature
    alongside the metadata that should be present in the archive.
    """

    allow_fallback_val = (
        _app.ALLOW_LPMBUILD_FALLBACK if allow_fallback is None else allow_fallback
    )
    root_path = _normalize_path(Path(root) if root is not None else Path(_app.DEFAULT_ROOT))

    plan_packages: list[dict[str, Any]] = []
    for pkg in packages:
        pkg_path = _normalize_path(Path(pkg))
        if not pkg_path.exists():
            raise FileNotFoundError(f"Package not found: {pkg_path}")
        meta, _ = _app.read_package_meta(pkg_path)
        if not meta:
            raise ValueError(f"Invalid package: {pkg_path.name} (no metadata)")

        plan_packages.append(
            {
                "path": str(pkg_path),
                "signature": str(pkg_path.with_suffix(pkg_path.suffix + ".sig")),
                "name": meta.name,
                "version": meta.version,
                "release": meta.release,
                "arch": meta.arch,
                "explicit": True,
            }
        )

    return {
        "packages": plan_packages,
        "root": str(root_path),
        "verify": bool(verify),
        "force": bool(force),
        "allow_fallback": bool(allow_fallback_val),
    }


__all__ = ["build_install_plan"]
