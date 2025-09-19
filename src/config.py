from __future__ import annotations

import json
import os
import shutil
import logging
from pathlib import Path
from typing import Dict, Mapping, Tuple

# =========================== Config / Defaults ================================
CONF_FILE = Path("/etc/lpm/lpm.conf")      # KEY=VALUE, e.g. ARCH=znver2
TEMPLATE_CONF = Path(__file__).resolve().parent.parent / "etc" / "lpm" / "lpm.conf"
STATE_DIR = Path(os.environ.get("LPM_STATE_DIR", "/var/lib/lpm"))
DB_PATH   = STATE_DIR / "state.db"
CACHE_DIR = STATE_DIR / "cache"
SNAPSHOT_DIR = STATE_DIR / "snapshots"
REPO_LIST = STATE_DIR / "repos.json"       # [{"name":"core","url":"file:///srv/repo","priority":10}, ...]
PIN_FILE  = STATE_DIR / "pins.json"        # {"hold":["pkg"], "prefer":{"pkg":"~=3.3"}}
HOOK_DIR  = Path("/usr/share/lpm/hooks")
SIGN_KEY  = Path("/etc/lpm/private/lpm_signing.pem")   # OpenSSL PEM private key for signing
TRUST_DIR = Path("/etc/lpm/trust")                     # dir of *.pem public keys for verification
DEFAULT_ROOT = "/"
UMASK = 0o22

# Module-level configuration cache; populated via _apply_conf()
CONF: Dict[str, str] = {}
ARCH = ""
OPT_LEVEL = "-O2"
MAX_SNAPSHOTS = 10
MAX_LEARNT_CLAUSES = 200
INSTALL_PROMPT_DEFAULT = "n"
ALLOW_LPMBUILD_FALLBACK = False
MARCH = "generic"
MTUNE = "generic"
CPU_VENDOR = ""
CPU_FAMILY = ""
DEVELOPER_MODE = False
ARCH_REPO_ENDPOINTS: Dict[str, str] = {}

_ARCH_REPO_DEFAULTS: Dict[str, str] = {
    "core": "https://gitlab.archlinux.org/archlinux/packaging/packages",
    "extra": "https://gitlab.archlinux.org/archlinux/packaging/packages",
    "community": "https://gitlab.archlinux.org/archlinux/packaging/packages",
    "multilib": "https://gitlab.archlinux.org/archlinux/packaging/packages",
    "testing": "https://gitlab.archlinux.org/archlinux/packaging/packages",
    "meta": "https://gitlab.archlinux.org/archlinux/packaging/meta",
}


def initialize_state() -> None:
    os.umask(UMASK)
    for d in (STATE_DIR, CACHE_DIR, SNAPSHOT_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if not REPO_LIST.exists():
        REPO_LIST.write_text("[]", encoding="utf-8")
    if not PIN_FILE.exists():
        PIN_FILE.write_text(json.dumps({"hold": [], "prefer": {}}, indent=2), encoding="utf-8")


def load_conf(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    out: Dict[str, str] = {}
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if "=" in ln:
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _get_bool(key: str, default: bool) -> bool:
    val = CONF.get(key)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _load_arch_repo_endpoints(conf: Mapping[str, str]) -> Dict[str, str]:
    endpoints = dict(_ARCH_REPO_DEFAULTS)

    raw_conf = conf.get("ARCH_REPO_ENDPOINTS")
    env_conf = os.environ.get("LPM_ARCH_ENDPOINTS")
    for source in (raw_conf, env_conf):
        if not source:
            continue
        try:
            parsed = json.loads(source)
        except json.JSONDecodeError:
            logging.warning("Invalid ARCH_REPO_ENDPOINTS JSON: %s", source)
            continue
        if isinstance(parsed, dict):
            for key, value in parsed.items():
                if isinstance(key, str) and isinstance(value, str) and value.strip():
                    endpoints[key.strip()] = value.strip()

    for key in list(endpoints):
        override = conf.get(f"ARCH_REPO_{key.upper()}")
        if override:
            endpoints[key] = override.strip()

    return endpoints


def _detect_cpu() -> Tuple[str, str, str, str]:
    """Return (march, mtune, vendor, family)."""
    vendor = family = model = ""
    flags: set[str] = set()
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            for line in f:
                if not vendor and line.startswith("vendor_id"):
                    vendor = line.split(":", 1)[1].strip()
                elif not family and line.startswith("cpu family"):
                    family = line.split(":", 1)[1].strip()
                elif not model and line.startswith("model") and line.split(":", 1)[0].strip() == "model":
                    model = line.split(":", 1)[1].strip()
                elif not flags and line.startswith("flags"):
                    flags = set(line.split(":", 1)[1].strip().split())
                if vendor and family and model and flags:
                    break
    except Exception:
        pass

    march = mtune = "generic"
    try:
        fam = int(family)
        mod = int(model)
    except Exception:
        fam = mod = None

    if vendor == "AuthenticAMD":
        if fam and fam >= 25:
            march = mtune = "znver4"
        elif fam and fam >= 24:
            march = mtune = "znver3"
        elif fam and fam >= 23:
            march = mtune = "znver2"
    elif vendor == "GenuineIntel":
        intel_fam6_models = {
            0x55: "x86-64-v4", 0x6A: "x86-64-v4", 0x6C: "x86-64-v4",
            0x7D: "x86-64-v4", 0x7E: "x86-64-v4", 0x8F: "x86-64-v4",
            0x9D: "x86-64-v4",
            0x3C: "x86-64-v3", 0x3F: "x86-64-v3", 0x45: "x86-64-v3",
            0x46: "x86-64-v3", 0x47: "x86-64-v3", 0x4E: "x86-64-v3",
            0x5E: "x86-64-v3", 0x8E: "x86-64-v3", 0x9E: "x86-64-v3",
            0xA5: "x86-64-v3", 0xA6: "x86-64-v3",
            0x2A: "x86-64-v2", 0x2D: "x86-64-v2", 0x3A: "x86-64-v2",
            0x3E: "x86-64-v2",
        }
        intel_features_v4 = {
            "avx512f", "avx512cd", "avx512bw", "avx512dq", "avx512vl"
        }
        intel_features_v3 = {"avx2", "bmi1", "bmi2", "fma"}
        intel_features_v2 = {"sse4_2", "popcnt", "cx16"}
        if fam == 6 and mod in intel_fam6_models:
            march = mtune = intel_fam6_models[mod]
        elif intel_features_v4.issubset(flags):
            march = mtune = "x86-64-v4"
        elif intel_features_v3.issubset(flags):
            march = mtune = "x86-64-v3"
        elif intel_features_v2.issubset(flags):
            march = mtune = "x86-64-v2"

    return march, mtune, vendor, family


def _normalize_cpu_type(val: str) -> str | None:
    """Return canonical dash form for supported x86-64 levels."""
    normalized = val.lower().replace("_", "").replace("-", "")
    if normalized in {"x8664v1", "x8664v2", "x8664v3", "x8664v4"}:
        return f"x86-64-v{normalized[-1]}"
    return None


def _init_cpu_settings() -> Tuple[str, str, str, str]:
    cpu_type = CONF.get("CPU_TYPE")
    if cpu_type:
        norm = _normalize_cpu_type(cpu_type)
        if norm:
            return norm, norm, "", ""
        logging.warning("Unrecognized CPU_TYPE %r; falling back to auto-detected CPU settings", cpu_type)
    return _detect_cpu()


def _apply_conf(conf: Mapping[str, str]) -> None:
    global CONF, ARCH, OPT_LEVEL, MAX_SNAPSHOTS, MAX_LEARNT_CLAUSES
    global INSTALL_PROMPT_DEFAULT, ALLOW_LPMBUILD_FALLBACK, MARCH, MTUNE
    global CPU_VENDOR, CPU_FAMILY, DEVELOPER_MODE, ARCH_REPO_ENDPOINTS

    CONF = dict(conf)
    ARCH = CONF.get("ARCH", os.uname().machine if hasattr(os, "uname") else "x86_64")

    OPT_LEVEL = CONF.get("OPT_LEVEL", "-O2")
    if OPT_LEVEL not in ("-Os", "-O2", "-O3", "-Ofast"):
        OPT_LEVEL = "-O2"

    try:
        MAX_SNAPSHOTS = max(0, int(CONF.get("MAX_SNAPSHOTS", "10")))
    except ValueError:
        MAX_SNAPSHOTS = 10

    try:
        MAX_LEARNT_CLAUSES = max(1, int(CONF.get("MAX_LEARNT_CLAUSES", "200")))
    except ValueError:
        MAX_LEARNT_CLAUSES = 200

    INSTALL_PROMPT_DEFAULT = CONF.get("INSTALL_PROMPT_DEFAULT", "n").lower()
    if INSTALL_PROMPT_DEFAULT not in ("y", "n"):
        INSTALL_PROMPT_DEFAULT = "n"

    ALLOW_LPMBUILD_FALLBACK = _get_bool("ALLOW_LPMBUILD_FALLBACK", False)

    env_dev = os.environ.get("LPM_DEVELOPER_MODE")
    if env_dev is not None:
        DEVELOPER_MODE = env_dev.strip().lower() in {"1", "true", "yes", "on"}
    else:
        DEVELOPER_MODE = _get_bool("DEVELOPER_MODE", False)

    ARCH_REPO_ENDPOINTS = _load_arch_repo_endpoints(CONF)

    MARCH, MTUNE, CPU_VENDOR, CPU_FAMILY = _init_cpu_settings()


def _normalize_key(key: str) -> str | None:
    cleaned = key.strip()
    if not cleaned:
        return None
    normalized = cleaned.upper()
    if not all(ch.isalnum() or ch == "_" for ch in normalized):
        return None
    return normalized


def _normalize_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    text = str(value)
    if "\n" in text or "\r" in text:
        parts = text.replace("\r", "\n").splitlines()
        text = " ".join(part.strip() for part in parts if part.strip())
    return text.strip()


def _load_template_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        pass

    try:
        return TEMPLATE_CONF.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def save_conf(settings: Mapping[str, object], path: Path = CONF_FILE) -> None:
    normalized: Dict[str, str] = {}
    for key, value in settings.items():
        norm_key = _normalize_key(key)
        if not norm_key:
            continue
        normalized[norm_key] = _normalize_value(value)

    base_lines = _load_template_lines(path)
    output_lines: list[str] = []
    used_keys: set[str] = set()

    for raw_line in base_lines:
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            output_lines.append(line)
            continue

        key_part, _, value_part = line.partition("=")
        norm_key = _normalize_key(key_part)
        if norm_key is None:
            output_lines.append(line)
            continue

        used_keys.add(norm_key)
        if norm_key in normalized:
            output_lines.append(f"{norm_key}={normalized[norm_key]}")
        else:
            output_lines.append(f"{norm_key}={value_part.strip()}")

    remaining = sorted(k for k in normalized if k not in used_keys)
    if remaining:
        if output_lines and output_lines[-1] != "":
            output_lines.append("")
        for key in remaining:
            output_lines.append(f"{key}={normalized[key]}")

    text = "\n".join(output_lines).rstrip()
    if text:
        text += "\n"
    else:
        text = "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

    _apply_conf(load_conf(path))


# Initialize globals on import
_apply_conf(load_conf(CONF_FILE))

# ================ Init System Detection ===============================================
def detect_init_system() -> str:
    """Detect which init system is active."""
    if shutil.which("systemctl") and os.path.isdir("/run/systemd/system"):
        return "systemd"
    if os.path.isdir("/etc/runit") or os.path.isdir("/etc/runit/runsvdir"):
        return "runit"
    if os.path.isdir("/etc/init.d"):
        if shutil.which("openrc"):
            return "openrc"
        return "sysv"
    return "unknown"


__all__ = [
    "CONF_FILE",
    "STATE_DIR",
    "DB_PATH",
    "CACHE_DIR",
    "SNAPSHOT_DIR",
    "REPO_LIST",
    "PIN_FILE",
    "HOOK_DIR",
    "SIGN_KEY",
    "TRUST_DIR",
    "DEFAULT_ROOT",
    "UMASK",
    "initialize_state",
    "load_conf",
    "save_conf",
    "CONF",
    "ARCH",
    "OPT_LEVEL",
    "MAX_SNAPSHOTS",
    "MAX_LEARNT_CLAUSES",
    "INSTALL_PROMPT_DEFAULT",
    "ALLOW_LPMBUILD_FALLBACK",
    "MARCH",
    "MTUNE",
    "CPU_VENDOR",
    "CPU_FAMILY",
    "DEVELOPER_MODE",
    "ARCH_REPO_ENDPOINTS",
    "detect_init_system",
]
