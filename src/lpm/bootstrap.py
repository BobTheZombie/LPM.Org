from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from tempfile import NamedTemporaryFile

from lpm.chroot import ChrootMountState, mount_chroot_api, umount_chroot_api
from typing import Any, Dict, Optional

import tomllib


class Stage(str, Enum):
    VALIDATE = "validate"
    PREPARE_DIRS = "prepare-dirs"
    SEED_LPM = "seed-lpm"
    INSTALL_BASE = "install-base"
    CONFIGURE_SYSTEM = "configure-system"
    GENERATE_INITRAMFS = "generate-initramfs"
    INSTALL_BOOTLOADER = "install-bootloader"
    FINAL_CHECK = "final-check"


STAGES: list[Stage] = [
    Stage.VALIDATE,
    Stage.PREPARE_DIRS,
    Stage.SEED_LPM,
    Stage.INSTALL_BASE,
    Stage.CONFIGURE_SYSTEM,
    Stage.GENERATE_INITRAMFS,
    Stage.INSTALL_BOOTLOADER,
    Stage.FINAL_CHECK,
]


@dataclass
class BootstrapConfig:
    target: Path
    hostname: Optional[str] = None
    timezone: Optional[str] = None
    locale: Optional[str] = None
    keymap: Optional[str] = None
    bootloader: Optional[str] = None
    kernel: Optional[str] = None
    config: Optional[Path] = None
    resume: bool = False
    dry_run: bool = False
    verbose: bool = False
    force: bool = False
    efi_dir: Optional[Path] = None
    boot_device: Optional[str] = None
    network: Optional[str] = None

    @property
    def state_path(self) -> Path:
        return self.target / "var/lib/lpm/bootstrap-state.json"


def _as_path(value: Any) -> Optional[Path]:
    if value in (None, ""):
        return None
    return Path(str(value))


def _parse_toml(path: Path) -> Dict[str, Any]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    section = data.get("bootstrap", data)
    return section if isinstance(section, dict) else {}


def load_config(cli_args: Any) -> BootstrapConfig:
    config_path = _as_path(getattr(cli_args, "config", None))
    toml_data: Dict[str, Any] = {}
    if config_path is not None and config_path.exists():
        toml_data = _parse_toml(config_path)

    def pick(name: str, default: Any = None) -> Any:
        cli_val = getattr(cli_args, name, None)
        return cli_val if cli_val is not None else toml_data.get(name, default)

    target = _as_path(pick("target"))
    if target is None:
        raise ValueError("bootstrap target is required")

    return BootstrapConfig(
        target=target,
        hostname=pick("hostname"),
        timezone=pick("timezone"),
        locale=pick("locale"),
        keymap=pick("keymap"),
        bootloader=pick("bootloader"),
        kernel=pick("kernel"),
        config=config_path,
        resume=bool(pick("resume", False)),
        dry_run=bool(pick("dry_run", False)),
        verbose=bool(pick("verbose", False)),
        force=bool(pick("force", False)),
        efi_dir=_as_path(pick("efi_dir")),
        boot_device=pick("boot_device"),
        network=pick("network"),
    )


def _log(cfg: BootstrapConfig, msg: str) -> None:
    if cfg.verbose or cfg.dry_run:
        print(f"[bootstrap] {msg}")


def _atomic_write_json(path: Path, payload: Dict[str, Any], dry_run: bool) -> None:
    if dry_run:
        print(f"[bootstrap][dry-run] write {path}: {json.dumps(payload, sort_keys=True)}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tf:
        json.dump(payload, tf, indent=2, sort_keys=True)
        tf.write("\n")
        tmp_name = tf.name
    os.replace(tmp_name, path)


def load_state(cfg: BootstrapConfig) -> Dict[str, Any]:
    if not cfg.state_path.exists():
        return {"completed_stages": []}
    return json.loads(cfg.state_path.read_text(encoding="utf-8"))


def save_state(cfg: BootstrapConfig, state: Dict[str, Any]) -> None:
    _atomic_write_json(cfg.state_path, state, cfg.dry_run)


def completed_stages(state: Dict[str, Any]) -> set[str]:
    raw = state.get("completed_stages", [])
    return {str(s) for s in raw if isinstance(s, str)}


def _run_stage(cfg: BootstrapConfig, stage: Stage, mount_state: ChrootMountState) -> None:
    _log(cfg, f"running stage={stage.value}")
    if stage == Stage.VALIDATE and not cfg.target.exists() and not cfg.dry_run:
        raise FileNotFoundError(f"target does not exist: {cfg.target}")
    if stage == Stage.PREPARE_DIRS:
        if cfg.dry_run:
            print("[bootstrap][dry-run] mount chroot api filesystems")
        else:
            mount_chroot_api(cfg.target, mount_state)
    if stage == Stage.SEED_LPM:
        if cfg.dry_run:
            print("[bootstrap][dry-run] package plan: seed lpm into target")
    if stage == Stage.INSTALL_BASE and cfg.dry_run:
        print("[bootstrap][dry-run] package plan: install base system")


def run_bootstrap(args: Any) -> int:
    cfg = load_config(args)
    state = load_state(cfg)
    done = completed_stages(state)
    mount_state = ChrootMountState()

    try:
        for stage in STAGES:
            if cfg.resume and stage.value in done:
                _log(cfg, f"skip completed stage={stage.value}")
                continue
            _run_stage(cfg, stage, mount_state)
            done.add(stage.value)
            save_state(cfg, {"completed_stages": sorted(done)})
        return 0
    finally:
        if not cfg.dry_run:
            try:
                umount_chroot_api(cfg.target, mount_state)
            except Exception as exc:
                print(f"[bootstrap] warning: failed to unmount API mounts: {exc}")
