from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from . import app as _app
from .atomic_io import atomic_replace
from .fs_ops import operation_phase
from .hooks import HookTransactionManager, load_hooks
from .priv import ensure_root_or_escalate

__all__ = ["installpkg"]

PkgMeta = _app.PkgMeta
ResolutionError = _app.ResolutionError


def installpkg(
    file: Path,
    root: Path = Path(_app.DEFAULT_ROOT),
    dry_run: bool = False,
    verify: bool = True,
    force: bool = False,
    explicit: bool = False,
    allow_fallback: bool = _app.ALLOW_LPMBUILD_FALLBACK,
    hook_transaction: Optional[HookTransactionManager] = None,
    register_event: bool = True,
) -> PkgMeta:
    """Install a local ``.zst`` package onto ``root``.

    This is the production-grade installer used by the CLI as well as the test
    suite. It closely mirrors the original implementation from
    :mod:`src.lpm.app` but lives in its own module so that privilege management
    can happen before any side effects occur.
    """

    root_path = Path(root)
    if not dry_run:
        try:
            normalized_root = root_path.resolve()
        except OSError:
            normalized_root = root_path
        default_root = Path(_app.DEFAULT_ROOT)
        if normalized_root in {default_root, Path("/")}:
            ensure_root_or_escalate("install packages")

    _app.PROTECTED = _app.load_protected()

    txn = hook_transaction
    owns_txn = False
    if txn is None and not dry_run:
        txn = HookTransactionManager(
            hooks=load_hooks(_app.LIBLPM_HOOK_DIRS),
            root=root_path,
            base_env={"LPM_ROOT": str(root_path)},
        )
        owns_txn = True

    file_path = Path(file)

    if file_path.suffix != _app.EXT:
        _app.die(f"{file_path.name} is not a {_app.EXT} package")
    try:
        with file_path.open("rb") as f:
            magic = f.read(4)
        if magic != b"\x28\xb5\x2f\xfd":
            _app.die(f"{file_path.name} is not a valid {_app.EXT} (bad magic header)")
    except Exception as exc:
        _app.die(f"Cannot read {file_path}: {exc}")

    sig = file_path.with_suffix(file_path.suffix + ".sig")
    if verify:
        if not sig.exists():
            _app.die(f"Missing signature: {sig}")
        _app.verify_signature(file_path, sig)

    meta, mani = _app.read_package_meta(file_path)
    if not meta:
        _app.die(f"Invalid package: {file_path.name} (no metadata)")
    _app.ok(
        f"Valid package: {meta.name}-{meta.version}-{meta.release}.{meta.arch}"
    )

    if not _app.arch_compatible(meta.arch, _app.ARCH):
        _app.die(f"Incompatible architecture: {meta.arch} (host: {_app.ARCH})")

    if meta.name in _app.PROTECTED and not force:
        _app.warn(
            f"{meta.name} is protected (from {_app.PROTECTED_FILE}) and cannot be "
            "installed/upgraded without --force"
        )
        return meta

    if not mani or all(entry["path"].startswith("/.lpm") for entry in mani):
        if meta.requires:
            _app.log(
                f"[meta] {meta.name} is a meta-package, resolving deps: "
                f"{', '.join(meta.requires)}"
            )
            universe = _app.build_universe()
            try:
                plan = _app.solve(meta.requires, universe)
            except ResolutionError as exc:  # pragma: no cover - passthrough
                raise ResolutionError(f"{meta.name}: {exc}")
            _app.do_install(
                plan,
                root_path,
                dry_run,
                verify,
                force,
                explicit=set(),
                allow_fallback=allow_fallback,
            )
            _app.ok(
                f"Installed meta-package {meta.name}-{meta.version}-{meta.release}."
                f"{meta.arch}"
            )
            return meta

    manifest_paths = _app._normalize_manifest_paths(mani)

    if dry_run:
        _app.log(
            f"[dry-run] Would install {meta.name}-{meta.version}-{meta.release}."
            f"{meta.arch}"
        )
        for entry in mani:
            print(f" -> {entry['path']} ({entry['size']} bytes)")
        return meta

    try:
        with operation_phase(privileged=True):
            conn = _app.db()
            row = conn.execute(
                "SELECT version, release FROM installed WHERE name=?",
                (meta.name,),
            ).fetchone()

        previous_version = row[0] if row else None
        previous_release = row[1] if row else None

        if txn is not None and register_event:
            operation = "Upgrade" if row else "Install"
            txn.add_package_event(
                name=meta.name,
                operation=operation,
                version=meta.version,
                release=meta.release,
                paths=manifest_paths,
            )

        if txn is not None:
            txn.ensure_pre_transaction()

        with _app.transaction(conn, f"install {meta.name}", dry_run):
            hook_env = {
                "LPM_PKG": meta.name,
                "LPM_VERSION": meta.version,
                "LPM_RELEASE": meta.release,
                "LPM_ROOT": str(root_path),
            }
            if previous_version is not None:
                hook_env["LPM_PREVIOUS_VERSION"] = previous_version
            if previous_release is not None:
                hook_env["LPM_PREVIOUS_RELEASE"] = previous_release

            _app.run_hook("pre_install", dict(hook_env))

            tmp_root = Path(
                tempfile.mkdtemp(prefix=f"lpm-{meta.name}-", dir="/tmp")
            )
            try:
                _app.extract_tar(file_path, tmp_root)

                for entry in mani:
                    staged = tmp_root / entry["path"].lstrip("/")
                    if not staged.exists() and not staged.is_symlink():
                        _app.die(f"Manifest missing file: {entry['path']}")

                    expected_hash = entry.get("sha256")
                    if staged.is_symlink() or "link" in entry:
                        try:
                            target = os.readlink(staged)
                        except OSError:
                            _app.die(f"Manifest missing file: {entry['path']}")

                        expected_target = entry.get("link")
                        if expected_target is not None and target != expected_target:
                            _app.die(
                                f"Link mismatch for {entry['path']}: expected "
                                f"{expected_target}, got {target}"
                            )

                        link_hash = hashlib.sha256(target.encode()).hexdigest()
                        payload_hash = None

                        if target.startswith("/"):
                            payload_candidate = tmp_root / target.lstrip("/")
                        else:
                            payload_candidate = staged.parent / target

                        resolved_payload: Optional[Path]
                        try:
                            resolved_payload = payload_candidate.resolve()
                        except (FileNotFoundError, RuntimeError, OSError):
                            resolved_payload = None

                        if resolved_payload is not None:
                            try:
                                resolved_payload.relative_to(tmp_root)
                            except ValueError:
                                resolved_payload = None

                        actual_hash: Optional[str]
                        payload_sum = (
                            _app.sha256sum(resolved_payload)
                            if resolved_payload
                            and resolved_payload.exists()
                            and resolved_payload.is_file()
                            else None
                        )

                        if (
                            payload_sum is not None
                            and (expected_hash is None or expected_hash == payload_sum)
                        ):
                            actual_hash = payload_sum
                        elif expected_hash == link_hash:
                            actual_hash = link_hash
                        elif payload_sum is not None:
                            actual_hash = payload_sum
                        else:
                            actual_hash = link_hash
                    else:
                        actual_hash = _app.sha256sum(staged)

                    if expected_hash is not None and actual_hash != expected_hash:
                        _app.die(
                            f"Hash mismatch for {entry['path']}: expected {expected_hash}, "
                            f"got {actual_hash}"
                        )

                with operation_phase(privileged=True):
                    replace_all = False
                    for entry in mani:
                        rel = entry["path"].lstrip("/")
                        src = tmp_root / rel
                        dest = root_path / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)

                        if src.is_dir():
                            dest.mkdir(parents=True, exist_ok=True)
                            continue

                        if dest.exists() or dest.is_symlink():
                            same = False
                            try:
                                if dest.is_file() and _app.sha256sum(dest) == entry["sha256"]:
                                    same = True
                            except Exception:
                                pass
                            if same:
                                _app.log(f"[skip] {rel} already up-to-date")
                                continue

                            def _remove_dest() -> None:
                                if dest.is_file() or dest.is_symlink():
                                    dest.unlink()
                                elif dest.is_dir():
                                    shutil.rmtree(dest)

                            if replace_all:
                                _remove_dest()
                            else:
                                while True:
                                    resp = input(
                                        "[conflict] {rel} exists. [R]eplace / [RA] Replace All / "
                                        "[S]kip / [A]bort? "
                                    ).strip().lower()
                                    if resp in ("r", "replace"):
                                        _remove_dest()
                                        break
                                    if resp in ("ra", "all", "replace all"):
                                        replace_all = True
                                        _remove_dest()
                                        break
                                    if resp in ("s", "skip"):
                                        _app.log(f"[skip] {rel}")
                                        src.unlink(missing_ok=True)
                                        continue
                                    if resp in ("a", "abort"):
                                        _app.die(
                                            f"Aborted install due to conflict at {rel}"
                                        )
                                    print("Please enter R, RA, S, or A.")

                        if not src.exists() and not src.is_symlink():
                            continue

                        if src.is_symlink():
                            target = os.readlink(src)
                            tmp_link = dest.with_name(f".{dest.name}.link")
                            try:
                                if tmp_link.exists() or tmp_link.is_symlink():
                                    tmp_link.unlink()
                            except FileNotFoundError:
                                pass
                            os.symlink(target, tmp_link)
                            os.replace(tmp_link, dest)
                            continue

                        st = src.stat()
                        file_mode = 0o755 if (st.st_mode & 0o111) else 0o644
                        with src.open("rb") as sf:
                            with atomic_replace(
                                dest, mode=file_mode, open_mode="wb"
                            ) as df:
                                shutil.copyfileobj(sf, df)

                    install_script_rel = "/.lpm-install.sh"
                    staged_script = tmp_root / install_script_rel.lstrip("/")
                    installed_script = root_path / install_script_rel.lstrip("/")
                    script_entry = next(
                        (e for e in mani if e["path"] == install_script_rel),
                        None,
                    )

                    script_to_run = None
                    if installed_script.exists():
                        script_to_run = installed_script
                    elif staged_script.exists():
                        script_to_run = staged_script

                    if script_to_run and os.access(script_to_run, os.X_OK):
                        env = os.environ.copy()
                        env.update(
                            {
                                "LPM_ROOT": str(root_path),
                                "LPM_PKG": meta.name,
                                "LPM_VERSION": meta.version,
                                "LPM_RELEASE": meta.release,
                            }
                        )

                        action = (
                            "upgrade" if previous_version is not None else "install"
                        )
                        env["LPM_INSTALL_ACTION"] = action
                        if previous_version is not None:
                            env["LPM_PREVIOUS_VERSION"] = previous_version
                        if previous_release is not None:
                            env["LPM_PREVIOUS_RELEASE"] = previous_release

                        def _format_version(ver: Optional[str], rel: Optional[str]) -> str:
                            if not ver:
                                return ""
                            return f"{ver}-{rel}" if rel else ver

                        new_full = _format_version(meta.version, meta.release)
                        old_full = _format_version(previous_version, previous_release)

                        argv = [str(script_to_run), action, new_full]
                        if action == "upgrade":
                            argv.append(old_full)

                        _app.log(
                            f"[lpm] Running embedded install script: {script_to_run}"
                        )
                        subprocess.run(argv, check=False, cwd=str(root_path), env=env)

                    if script_entry and not script_entry.get("keep", False):
                        for candidate in (installed_script, staged_script):
                            try:
                                candidate.unlink()
                            except FileNotFoundError:
                                pass
                        mani = [
                            entry for entry in mani if entry["path"] != install_script_rel
                        ]

                conn.execute(
                    "REPLACE INTO installed("
                    "name,version,release,arch,provides,symbols,requires,manifest,explicit,install_time"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        meta.name,
                        meta.version,
                        meta.release,
                        meta.arch,
                        json.dumps([meta.name] + meta.provides),
                        json.dumps(meta.symbols),
                        json.dumps(meta.requires),
                        json.dumps(mani),
                        1 if explicit else 0,
                        int(time.time()),
                    ),
                )
                action = "upgrade" if previous_version is not None else "install"
                conn.execute(
                    "INSERT INTO history("
                    "ts,action,name,from_ver,to_ver,details"
                    ") VALUES(?,?,?,?,?,?)",
                    (
                        int(time.time()),
                        action,
                        meta.name,
                        previous_version,
                        meta.version,
                        json.dumps(dataclasses.asdict(meta)),
                    ),
                )
            finally:
                shutil.rmtree(tmp_root, ignore_errors=True)

            _app.run_hook("post_install", dict(hook_env))

            if previous_version is not None:
                _app.run_hook("post_upgrade", dict(hook_env))

            with operation_phase(privileged=True):
                _app.handle_service_files(meta.name, root_path, mani)

    except (PermissionError, OSError) as exc:
        if _app._is_permission_error(exc):
            _app._handle_permission_denied(
                "install packages",
                "install transaction",
                "Installing packages requires root privileges.",
            )
        raise

    if txn is not None and owns_txn:
        txn.run_post_transaction()

    _app.ok(
        f"Installed {meta.name}-{meta.version}-{meta.release}.{meta.arch}"
    )
    return meta
