from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Dict, Tuple

# =========================== Config / Defaults ================================
CONF_FILE = Path("/etc/lpm/lpm.conf")      # KEY=VALUE, e.g. ARCH=znver2
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


# ================ CONFIG LOADER:: /etc/lpm/lpm.conf ====
CONF = load_conf(CONF_FILE)
ARCH = CONF.get("ARCH", os.uname().machine if hasattr(os, "uname") else "x86_64")

# --- Optimization level (-O2 etc.) ---
OPT_LEVEL = CONF.get("OPT_LEVEL", "-O2")
if OPT_LEVEL not in ("-Os", "-O2", "-O3", "-Ofast"):
    OPT_LEVEL = "-O2"

# --- Snapshot retention ---
try:
    MAX_SNAPSHOTS = max(0, int(CONF.get("MAX_SNAPSHOTS", "10")))
except ValueError:
    MAX_SNAPSHOTS = 10

# --- SAT solver learnt clause limit ---
try:
    MAX_LEARNT_CLAUSES = max(1, int(CONF.get("MAX_LEARNT_CLAUSES", "200")))
except ValueError:
    MAX_LEARNT_CLAUSES = 200

# --- Default response for install prompts ---
INSTALL_PROMPT_DEFAULT = CONF.get("INSTALL_PROMPT_DEFAULT", "n").lower()
if INSTALL_PROMPT_DEFAULT not in ("y", "n"):
    INSTALL_PROMPT_DEFAULT = "n"


def _detect_cpu() -> Tuple[str, str, str, str]:
    """Return (march, mtune, vendor, family)."""
    vendor = family = ""  # defaults
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            for line in f:
                if not vendor and (
                    line.startswith("vendor_id")
                    or line.startswith("CPU implementer")
                    or line.startswith("uarch")
                    or line.lower().startswith("vendor")
                ):
                    vendor = line.split(":", 1)[1].strip()
                elif not family and (
                    line.startswith("cpu family")
                    or line.startswith("CPU architecture")
                    or line.startswith("isa")
                ):
                    family = line.split(":", 1)[1].strip()
                if vendor and family:
                    break
    except Exception:
        pass

    march = mtune = "generic"
    try:
        fam = int(family)
    except Exception:
        fam = None

    if vendor == "AuthenticAMD":
        if fam and fam >= 25:
            march = mtune = "znver4"
        elif fam and fam >= 24:
            march = mtune = "znver3"
        elif fam and fam >= 23:
            march = mtune = "znver2"
    elif vendor == "GenuineIntel":
        if fam and fam >= 6:
            march = mtune = "x86-64-v3"
    elif vendor in ("0x41", "ARM"):
        if fam and fam >= 9:
            march = mtune = "armv9-a"
        elif fam and fam >= 8:
            march = mtune = "armv8-a"
        elif fam and fam >= 7:
            march = mtune = "armv7-a"
    elif family.startswith("rv64"):
        march = mtune = "rv64gc"
    elif family.startswith("rv32"):
        march = mtune = "rv32gc"

    return march, mtune, vendor, family


MARCH, MTUNE, CPU_VENDOR, CPU_FAMILY = _detect_cpu()


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
    "CONF",
    "ARCH",
    "OPT_LEVEL",
    "MAX_SNAPSHOTS",
    "MAX_LEARNT_CLAUSES",
    "INSTALL_PROMPT_DEFAULT",
    "MARCH",
    "MTUNE",
    "CPU_VENDOR",
    "CPU_FAMILY",
    "detect_init_system",
]
