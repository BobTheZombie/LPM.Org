from __future__ import annotations

import json
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    from .hooks import HookTransactionManager


def removepkg(
    name: str,
    root: Optional[Path] = None,
    dry_run: bool = False,
    force: bool = False,
    hook_transaction: Optional["HookTransactionManager"] = None,
    register_event: bool = True,
) -> None:
    from .app import (
        DEFAULT_ROOT,
        LIBLPM_HOOK_DIRS,
        PROTECTED_FILE,
        _handle_permission_denied,
        _is_permission_error,
        _normalize_manifest_paths,
        _remove_installed_package,
        db,
        ensure_root_or_escalate,
        load_protected,
        ok,
        run_hook,
        transaction,
        warn,
    )
    from .hooks import HookTransactionManager, load_hooks
    from . import app as app_module

    app_module.PROTECTED = load_protected()

    root_path = Path(root or DEFAULT_ROOT)
    txn = hook_transaction
    owns_txn = False
    if txn is None and not dry_run:
        txn = HookTransactionManager(
            hooks=load_hooks(LIBLPM_HOOK_DIRS),
            root=root_path,
            base_env={"LPM_ROOT": str(root_path)},
        )
        owns_txn = True

    if name in app_module.PROTECTED and not force:
        warn(f"{name} is protected (from {PROTECTED_FILE}) and cannot be removed without --force")
        return

    conn = db()
    cur = conn.execute("SELECT version, release, manifest FROM installed WHERE name=?", (name,))
    row = cur.fetchone()
    if not row:
        warn(f"{name} not installed")
        return

    version, release, manifest_json = row
    manifest = json.loads(manifest_json) if manifest_json else []
    meta = {"name": name, "version": version, "release": release, "manifest": manifest}

    manifest_paths = _normalize_manifest_paths(manifest)

    if txn is not None and register_event and not dry_run:
        txn.add_package_event(
            name=name,
            operation="Remove",
            version=version,
            release=release,
            paths=manifest_paths,
        )

    if not dry_run:
        ensure_root_or_escalate("remove packages")

    if txn is not None and not dry_run:
        txn.ensure_pre_transaction()

    try:
        with transaction(conn, f"remove {name}", dry_run):
            run_hook("pre_remove", {"LPM_PKG": name, "LPM_ROOT": str(root_path)})
            _remove_installed_package(meta, root_path, dry_run, conn)
            run_hook("post_remove", {"LPM_PKG": name, "LPM_ROOT": str(root_path)})
    except (PermissionError, OSError) as exc:
        if _is_permission_error(exc):
            _handle_permission_denied(
                "remove packages",
                "remove transaction",
                "Removing packages requires root privileges.",
            )
        raise

    if txn is not None and owns_txn and not dry_run:
        txn.run_post_transaction()

    ok(f"Removed {name}-{version}")

