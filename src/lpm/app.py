Warning: truncated output (original token count: 67618)
Total output lines: 7187

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lpm — Linux Package Manager with SAT solver, native .lpm packaging, signatures, and .lpmbuild support.

Features:
- SAT-grade resolver (CNF + DPLL): versioned deps, provides (incl. versioned), conflicts, obsoletes, alternatives, recommends/suggests.
- LFS-friendly: --root installs (chroot/DESTDIR), no systemd/RPM deps.
- .lpm builder: tar + zstd with embedded .lpm-meta.json & .lpm-manifest.json (sha256 + size).
- Sign & verify: OpenSSL signing (PEM private key) and verification (trusted public keys dir).
- Repo handling: repoadd/repodel/repolist, fetch JSON indices, genindex from a dir of .lpm packages.
- State & safety: SQLite installed DB, file manifests, history, pins (hold/prefer), verify command.
- Build scripts: .lpmbuild (bash) via lpm buildpkg.

License: MIT
"""

from __future__ import annotations
import argparse, contextlib, dataclasses, errno, fnmatch, hashlib, io, json, os, re, shlex, shutil, sqlite3, stat, subprocess, sys, tarfile, tempfile, time, urllib.parse
import importlib.util
from datetime import datetime, timezone
from email.parser import Parser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Iterable, Callable, BinaryIO, Mapping
from collections import deque

if __package__ in {None, ""}:
    src_root = Path(__file__).resolve().parents[1]
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))
    __package__ = "lpm"

_LPM_OVERRIDE_MODULES: list[object] = []
for _module_name in ("lpm", "src.lpm"):
    _module = sys.modules.get(_module_name)
    if _module is not None:
        _LPM_OVERRIDE_MODULES.append(_module)
if importlib.util.find_spec("zstandard") is None:  # pragma: no cover - fallback for test environment
    from . import _zstd_stub as zstd
else:
    import zstandard as zstd
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import Specifier, SpecifierSet
from packaging.utils import canonicalize_name

_TAR_EXTRACT_ERRORS = (tarfile.ExtractError,)
if hasattr(tarfile, "FilterError"):
    _TAR_EXTRACT_ERRORS = _TAR_EXTRACT_ERRORS + (tarfile.FilterError,)  # type: ignore[attr-defined]

# =========================== Runtime metadata =================================
_ENV_NAME = "LPM_NAME"
_ENV_VERSION = "LPM_VERSION"
_ENV_BUILD = "LPM_BUILD"
_ENV_BUILD_DATE = "LPM_BUILD_DATE"
_ENV_DEVELOPER = "LPM_DEVELOPER"
_ENV_URL = "LPM_URL"

_DEFAULT_NAME = "LPM"
_FALLBACK_VERSION = "1.0.0"
_FALLBACK_BUILD = "development"
def _format_timestamp(value: float) -> str:
    return (
        datetime.fromtimestamp(value, tz=timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _default_build_date() -> str:
    override = os.environ.get("SOURCE_DATE_EPOCH")
    if override:
        try:
            return _format_timestamp(int(override))
        except Exception:
            pass

    try:
        mtime = Path(__file__).resolve().stat().st_mtime
    except Exception:
        return ""

    try:
        return _format_timestamp(mtime)
    except Exception:
        return ""


def _load_build_metadata() -> Dict[str, str]:
    candidates: List[Path] = []

    env_path = os.environ.get("LPM_BUILD_INFO")
    if env_path:
        candidates.append(Path(env_path))

    module_path = Path(__file__).resolve()
    package_module = sys.modules.get("lpm") or sys.modules.get("src.lpm")
    if package_module is None and _LPM_OVERRIDE_MODULES:
        package_module = _LPM_OVERRIDE_MODULES[0]
    if package_module is not None:
        package_file = getattr(package_module, "__file__", None)
        if package_file:
            module_path = Path(package_file).resolve()
    candidates.append(module_path.with_name("_build_info.json"))

    parents = module_path.parents
    if len(parents) >= 3:
        candidates.append(parents[2] / "build" / "build-info.json")

    try:
        exe_path = Path(sys.argv[0]).resolve()
    except Exception:
        exe_path = None
    else:
        candidates.append(exe_path.parent / ".." / "share" / "lpm" / "build-info.json")

    candidates.append(Path("/usr/share/lpm/build-info.json"))

    seen: Set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if not resolved.is_file():
            continue
        try:
            data = json.loads(resolved.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict):
            return {
                str(key): str(value)
                for key, value in data.items()
                if isinstance(key, str) and isinstance(value, str)
            }
    return {}


_BUILD_METADATA = _load_build_metadata()
_DEFAULT_BUILD_DATE = _BUILD_METADATA.get("build_date") or _default_build_date()
_DEFAULT_VERSION = _BUILD_METADATA.get("version") or _DEFAULT_BUILD_DATE or _FALLBACK_VERSION
_DEFAULT_BUILD = _BUILD_METADATA.get("build") or _FALLBACK_BUILD
_DEFAULT_DEVELOPER = "Derek Midkiff aka BobTheZombie"
_DEFAULT_URL = "https://github.com/BobTheZombie/LPM"

__title__ = os.environ.get(_ENV_NAME, _DEFAULT_NAME)
__version__ = os.environ.get(_ENV_VERSION, _DEFAULT_VERSION)
__build__ = os.environ.get(_ENV_BUILD, _DEFAULT_BUILD)
__build_date__ = os.environ.get(_ENV_BUILD_DATE, _DEFAULT_BUILD_DATE)
__developer__ = os.environ.get(_ENV_DEVELOPER, _DEFAULT_DEVELOPER)
__url__ = os.environ.get(_ENV_URL, _DEFAULT_URL)

LPMSPEC_API_VERSION = "1.0"


def _refresh_runtime_metadata() -> None:
    build_metadata = _load_build_metadata()
    default_build_date = build_metadata.get("build_date") or _default_build_date()
    default_version = build_metadata.get("version") or default_build_date or _FALLBACK_VERSION
    default_build = build_metadata.get("build") or _FALLBACK_BUILD

    global __title__, __version__, __build__, __build_date__, __developer__, __url__
    __title__ = os.environ.get(_ENV_NAME, _DEFAULT_NAME)
    __version__ = os.environ.get(_ENV_VERSION, default_version)
    __build__ = os.environ.get(_ENV_BUILD, default_build)
    __build_date__ = os.environ.get(_ENV_BUILD_DATE, default_build_date)
    __developer__ = os.environ.get(_ENV_DEVELOPER, _DEFAULT_DEVELOPER)
    __url__ = os.environ.get(_ENV_URL, _DEFAULT_URL)


def get_runtime_metadata() -> Dict[str, str]:
    """Return runtime metadata describing the current LPM build.

    The module level ``__title__``, ``__version__``, ``__build__``,
    ``__build_date__``, ``__developer__``, and ``__url__`` constants default to
    static fallback values but can be overridden via the corresponding
    ``LPM_*`` environment variables. Importing :mod:`lpm` merely exposes these
    values without triggering the heavier initialization logic below.
    """

    _refresh_runtime_metadata()
    return {
        "name": __title__,
        "version": __version__,
        "build": __build__,
        "build_date": __build_date__,
        "developer": __developer__,
        "url": __url__,
    }

from .config import (
    ARCH,
    ALLOW_LPMBUILD_FALLBACK,
    CACHE_DIR,
    SOURCE_CACHE_DIR,
    CONF,
    CONF_FILE,
    CPU_FAMILY,
    CPU_VENDOR,
    DB_PATH,
    DEFAULT_ROOT,
    FETCH_MAX_WORKERS,
    HOOK_DIR,
    IO_BUFFER_SIZE,
    LIBLPM_HOOK_DIRS,
    MAX_LEARNT_CLAUSES,
    INSTALL_PROMPT_DEFAULT,
    MAX_SNAPSHOTS,
    MARCH,
    MTUNE,
    OPT_LEVEL,
    ENABLE_CPU_OPTIMIZATIONS,
    PIN_FILE,
    REPO_LIST,
    SIGN_KEY,
    SNAPSHOT_DIR,
    STATE_DIR,
    TRUST_DIR,
    detect_init_system,
    initialize_state,
)
from fs import read_json, write_json, urlread
from installgen import generate_install_script
from first_run_ui import FirstRunSetupError, run_first_run_wizard
import maintainer_mode
from . import config as _config
from .atomic_io import atomic_replace, safe_write
from .fs_ops import operation_phase, prepare_directory
from .privileges import privilege_info, privileged_section, privileges_enabled, require_root
from .resolver import CNF, CDCLSolver
from .hooks import HookExecutionError, HookFailureMode, HookTransactionManager, _ensure_executable, load_hooks
from .delta import apply_delta, find_cached_by_sha, file_sha256, zstd_version, version_at_least
from . import bootstrap
from . import chroot_helpers

# =========================== Protected packages ===============================
PROTECTED_FILE = Path("/etc/lpm/protected.json")

def load_protected() -> List[str]:
    default = ["glibc", "zlib", "lpm"]
    if not PROTECTED_FILE.exists():
        try:
            with operation_phase(privileged=True):
                PROTECTED_FILE.parent.mkdir(parents=True, exist_ok=True)
                write_json(PROTECTED_FILE, {"protected": default})
        except Exception:
            return default
    try:
        data = read_json(PROTECTED_FILE)
        return list(set(data.get("protected", default)))
    except Exception:
        return default

PROTECTED = load_protected()

# =========================== Logging/IO utils =================================
CYAN   = "\033[1;36m"
PURPLE = "\033[1;35m"
GREEN  = "\033[1;32m"
RED    = "\033[1;31m"
RESET  = "\033[0m"

def _resolve_lpm_attr(name: str, default):
    from ._compat import facades

    for module_name in ("lpm", "src.lpm"):
        module = sys.modules.get(module_name)
        if module is not None and module not in _LPM_OVERRIDE_MODULES:
            _LPM_OVERRIDE_MODULES.append(module)

    current_modules = [sys.modules.get("lpm"), sys.modules.get("src.lpm")]
    ordered_modules = [m for m in current_modules if m is not None]
    for module in _LPM_OVERRIDE_MODULES:
        if module not in ordered_modules:
            ordered_modules.append(module)
    for module in facades:
        if module not in ordered_modules:
            ordered_modules.append(module)

    for module in ordered_modules:
        if name in getattr(module, "__dict__", {}):
            return module.__dict__[name]
    for module in ordered_modules:
        try:
            return getattr(module, name)
        except AttributeError:
            continue
    return default


def log(msg: str):
    override = _resolve_lpm_attr("log", None)
    if override is not None and override is not log:
        return override(msg)
    print(f"{PURPLE}{msg}{RESET}", file=sys.stderr)

def die(msg: str, code: int = 2):
    override = _resolve_lpm_attr("die", None)
    if override is not None and override is not die:
        return override(msg, code)
    print(f"{RED}[ERROR]{RESET} {msg}", file=sys.stderr)
    sys.exit(code)

def ok(msg: str):
    override = _resolve_lpm_attr("ok", None)
    if override is not None and override is not ok:
        return override(msg)
    print(f"{GREEN}[OK]{RESET} {msg}", file=sys.stderr)

def warn(msg: str):
    override = _resolve_lpm_attr("warn", None)
    if override is not None and override is not warn:
        return override(msg)
    print(f"{CYAN}[WARN]{RESET} {msg}", file=sys.stderr)


_DELTA_MODE = _config.USE_DELTAS


@contextlib.contextmanager
def _delta_mode(mode: str):
    global _DELTA_MODE
    previous = _DELTA_MODE
    _DELTA_MODE = mode
    try:
        yield
    finally:
        _DELTA_MODE = previous


def _current_delta_mode() -> str:
    mode = (_DELTA_MODE or "auto").lower()
    if mode not in {"auto", "always", "never"}:
        return "auto"
    return mode

def print_build_summary(meta: PkgMeta, out: Path, duration: float, deps: int, phases: int):
    """Print a Meson-like build summary table."""
    rows = [
        ("Name", meta.name),
        ("Version", meta.version),
        ("Arch", meta.arch),
        ("Output", out),
        ("Build time", f"{duration:.2f}s"),
        ("Dependencies", deps),
        ("Phases", phases),
    ]
    width = max(len(k) for k, _ in rows)
    print("\nSummary")
    for k, v in rows:
        print(f"  {k:<{width}} {v}")

# Specific exception for dependency resolution failures
class ResolutionError(Exception):
    """Raised when dependency resolution fails."""
    pass

# Progress bar wrapper
from tqdm import tqdm


class _TrackedTqdm(tqdm):
    """A ``tqdm`` subclass that records start/end times and completed count."""

    def __enter__(self):
        self.start_time = time.time()
        return super().__enter__()

    def __exit__(self, exc_type, exc, tb):
        self.end_time = time.time()
        self.completed = self.n
        return super().__exit__(exc_type, exc, tb)

    def set_description(self, *args, **kwargs):  # pragma: no cover - passthrough shim
        setter = getattr(super(), "set_description", None)
        if setter is not None:
            return setter(*args, **kwargs)
        # ``tqdm`` is stubbed in the test environment; gracefully accept the call
        if args:
            self.desc = args[0]
        return None


def progress_bar(
    iterable,
    *,
    desc: str = "Processing",
    unit: str = "item",
    total: Optional[int] = None,
    colour: str = "cyan",
    bar_format: Optional[str] = None,
    leave: bool = True,
    mode: str = "bar",
    track: bool = False,
    **kwargs,
):
    """Return a ``tqdm`` progress bar with centralized styling.

    Parameters map directly to the underlying ``tqdm`` arguments. Any
    additional keyword arguments are forwarded as-is, while enforcing a
    consistent width and default colour.

    Args:
        iterable: Iterable to wrap.
        desc: Description shown alongside the progress bar.
        unit: Unit of measurement for each iteration.
        total: Expected number of items.
        colour: Colour of the bar (if displayed).
        bar_format: Custom ``tqdm`` ``bar_format`` string.
        leave: Whether to keep the progress bar after completion.
        mode: ``"bar"`` for the standard ``tqdm`` bar or ``"ninja"`` for
            Ninja-style output that disables the graphical bar and displays
            ``"[ n/total ] desc"``.
    """

    if mode == "ninja":
        bar_format = bar_format or "[ {n}/{total} ] {desc}"

    cls = _TrackedTqdm if track else tqdm

    bar = cls(
        iterable,
        desc=desc,
        unit=unit,
        total=total,
        ncols=80,
        colour=colour,
        bar_format=bar_format,
        leave=leave,
        **kwargs,
    )

    if not hasattr(bar, "set_description"):
        bar.set_description = lambda *args, **kwargs: None  # type: ignore[attr-defined]

    return bar

# ============================ Build Isolation =======================
def sandboxed_run(
    func: str,
    cwd: Path,
    env: dict,
    script_path: Path,
    stagedir: Path,
    buildroot: Path,
    srcroot: Path,
    *,
    aliases: Iterable[str] = (),
):
    """Run build function inside sandbox depending on SANDBOX_MODE.

    Supports: none, fakeroot, bwrap.
    """
    mode = CONF.get("SANDBOX_MODE", "none").lower()
    script_abs = script_path.resolve()
    script_quoted = shlex.quote(str(script_abs))

    candidates = [func, *aliases]
    candidate_list = " ".join(shlex.quote(name) for name in candidates)
    pick_wrapper = (
        "_pick() {\n"
        "    local p=\"$1\"\n"
        "    shift || true\n"
        "    if [ -z \"${pkgdir:-}\" ]; then\n"
        "        echo \"_pick: pkgdir is not set\" >&2\n"
        "        return 1\n"
        "    fi\n"
        "    if [ -n \"${SRCROOT:-}\" ]; then\n"
        "        srcdir=\"$SRCROOT\"\n"
        "    fi\n"
        "    if [ -z \"${srcdir:-}\" ]; then\n"
        "        echo \"_pick: SRCROOT is not set\" >&2\n"
        "        return 1\n"
        "    fi\n"
        "    if [ -z \"${p:-}\" ]; then\n"
        "        echo \"_pick: missing package name (p)\" >&2\n"
        "        return 1\n"
        "    fi\n"
        "    local pkgdir_root=\"${pkgdir%/}\"\n"
        "    local srcdir_root=\"${srcdir%/}\"\n"
        "    local pkgdir_real\n"
        "    local srcroot_real\n"
        "    pkgdir_real=\"$(realpath -m -- \"$pkgdir_root\")\"\n"
        "    srcroot_real=\"$(realpath -m -- \"$srcdir_root\")\"\n"
        "    local f\n"
        "    for f in \"$@\"; do\n"
        "        if [ -z \"$f\" ]; then\n"
        "            echo \"_pick: empty path for p=$p\" >&2\n"
        "            return 1\n"
        "        fi\n"
        "        local rel\n"
        "        if [ \"${f#/}\" != \"$f\" ]; then\n"
        "            if [ \"$f\" = \"$pkgdir_root\" ]; then\n"
        "                echo \"_pick: refusing to move pkgdir itself (p=$p, f=$f)\" >&2\n"
        "                return 1\n"
        "            elif [ \"${f#$pkgdir_root/}\" != \"$f\" ]; then\n"
        "                rel=\"${f#$pkgdir_root/}\"\n"
        "            else\n"
        "                rel=\"${f#/}\"\n"
        "            fi\n"
        "        else\n"
        "            rel=\"$f\"\n"
        "        fi\n"
        "        if [ -z \"$rel\" ]; then\n"
        "            echo \"_pick: empty relative path (p=$p, f=$f)\" >&2\n"
        "            return 1\n"
        "        fi\n"
        "        local src_path=\"$pkgdir_root/$rel\"\n"
        "        if [ ! -e \"$src_path\" ] && [ ! -L \"$src_path\" ]; then\n"
        "            echo \"_pick: source does not exist (p=$p, f=$f, src=$src_path)\" >&2\n"
        "            return 1\n"
        "        fi\n"
        "        local src_resolved\n"
        "        src_resolved=\"$(realpath -m -- \"$src_path\")\"\n"
        "        case \"$src_resolved\" in\n"
        "            \"$pkgdir_real\"|\"$pkgdir_real\"/*) ;;\n"
        "            *)\n"
        "                echo \"_pick: source resolves outside pkgdir (p=$p, f=$f, src=$src_resolved, pkgdir=$pkgdir_real)\" >&2\n"
        "                return 1\n"
        "                ;;\n"
        "        esac\n"
        "        local dest_root=\"$srcdir_root/$p\"\n"
        "        local dest_root_resolved\n"
        "        dest_root_resolved=\"$(realpath -m -- \"$dest_root\")\"\n"
        "        case \"$dest_root_resolved\" in\n"
        "            \"$srcroot_real\"|\"$srcroot_real\"/*) ;;\n"
        "            *)\n"
        "                echo \"_pick: destination root resolves outside SRCROOT (p=$p, f=$f, root=$dest_root_resolved, SRCROOT=$srcroot_real)\" >&2\n"
        "                return 1\n"
        "                ;;\n"
        "        esac\n"
        "        local dest_path=\"$dest_root/$rel\"\n"
        "        local dest_resolved\n"
        "        dest_resolved=\"$(realpath -m -- \"$dest_path\")\"\n"
        "        case \"$dest_resolved\" in\n"
        "            \"$dest_root_resolved\"|\"$dest_root_resolved\"/*) ;;\n"
        "            *)\n"
        "                echo \"_pick: destination resolves outside srcroot (p=$p, f=$f, dest=$dest_resolved, srcroot=$dest_root_resolved)\" >&2\n"
        "                return 1\n"
        "                ;;\n"
        "        esac\n"
        "        mkdir -p \"$(dirname \"$dest_path\")\"\n"
        "        mv -- \"$src_path\" \"$dest_path\"\n"
        "        local prune_dir\n"
        "        prune_dir=\"$(dirname \"$src_path\")\"\n"
        "        while [ \"$prune_dir\" != \"$pkgdir_real\" ] && [ \"$prune_dir\" != \"/\" ]; do\n"
        "            rmdir \"$prune_dir\" 2>/dev/null || true\n"
        "            prune_dir=\"$(dirname \"$prune_dir\")\"\n"
        "        done\n"
        "    done\n"
        "}\n"
        "pick() {\n"
        "    _pick \"$@\"\n"
        "}\n"
    )
    wrapper_body = (
        "__lpm_run_phase() {\n"
        "    local __lpm_requested=\"$1\"\n"
        "    shift || true\n"
        f"    local __lpm_candidates=({candidate_list})\n"
        "    local __lpm_phase_name\n"
        "    for __lpm_phase_name in \"${__lpm_candidates[@]}\"; do\n"
        "        local __lpm_phase_def\n"
        "        if __lpm_phase_def=\"$(declare -f \"$__lpm_phase_name\")\"; then\n"
        "            local __lpm_phase_wrapper=\"__lpm_phase_${__lpm_phase_name}\"\n"
        "            eval \"${__lpm_phase_def/$__lpm_phase_name/$__lpm_phase_wrapper}\"\n"
        "            unset -f \"$__lpm_phase_name\"\n"
        "            \"$__lpm_phase_wrapper\" \"$@\"\n"
        "            return\n"
        "        fi\n"
        "    done\n"
        "    \"$__lpm_requested\" \"$@\"\n"
        "}\n"
        f"__lpm_run_phase {shlex.quote(func)}\n"
    )
    wrapper = f"set -e\n{pick_wrapper}\nsource {script_quoted}\n{wrapper_body}"

    run_env = dict(env)

    if mode == "fakeroot":
        cmd = ["fakeroot", "bash", "-c", wrapper]
        subprocess.run(cmd, check=True, env=run_env, cwd=str(cwd))
        return

    if mode == "bwrap":
        # bwrap isolates FS: read-only root, only bind staging/build/src dirs
        host_destdir = run_env.get("DESTDIR") or str(stagedir)
        host_pkgdir = run_env.get("pkgdir") or host_destdir
        host_buildroot = run_env.get("BUILDROOT") or str(buildroot)
        host_srcroot = run_env.get("SRCROOT") or str(srcroot)
        run_env.update(
            {
                "DESTDIR": "/pkgdir",
                "pkgdir": "/pkgdir",
                "BUILDROOT": "/build",
                "SRCROOT": "/src",
                "LPM_HOST_DESTDIR": host_destdir,
                "LPM_HOST_PKGDIR": host_pkgdir,
                "LPM_HOST_BUILDROOT": host_buildroot,
                "LPM_HOST_SRCROOT": host_srcroot,
            }
        )
        cmd = [
            "bwrap",
            "--ro-bind",
            "/",
            "/",
            "--bind",
            str(stagedir),
            "/pkgdir",
            "--bind",
            str(buildroot),
            "/build",
            "--bind",
            str(srcroot),
            "/src",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--unshare-all",
            "--share-net",  # allow networking (remove for full isolation)
            "--die-with-parent",
            "bash",
            "-c",
            f"set -e\ncd /src\n{pick_wrapper}\nsource {script_quoted}\n{wrapper_body}",
        ]
        subprocess.run(cmd, check=True, env=run_env, cwd=str(cwd))
        return

    # Default: no sandbox
    cmd = ["bash", "-c", wrapper]
    subprocess.run(cmd, check=True, env=run_env, cwd=str(cwd))

# ================ PACKAGING  ================
# Hard-locked to .zst
EXT = ".zst"

# =========================== Version / Semver ops =============================
SEMVER_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+~].*)?$")
def parse_semver(v: str) -> Tuple[int,int,int]:
    m = SEMVER_RE.match(str(v).strip())
    if not m: return (0,0,0)
    return tuple(int(x) if x is not None else 0 for x in m.groups())
def cmp_semver(a: str, b: str) -> int:
    ta, tb = parse_semver(a), parse_semver(b)
    return (ta > tb) - (ta < tb)
def satisfies(ver: str, cons: str) -> bool:
    """Supports =, ==, >=, <=, >, <, ~=, and 'X.*' (e.g., 3.3.* == ~=3). Comma-separated parts are ANDed."""
    if not cons or cons.strip() in ("", "*"): return True
    for part in [p.strip() for p in cons.split(",") if p.strip()]:
        if part.endswith(".*"): op, val = "~=", part[:-2]
        elif part.startswith("=="): op, val = "==", part[2:].strip()
        elif part.startswith("="):  op, val = "==", part[1:].strip()
        elif part.startswith(">="): op, val = ">=", part[2:].strip()
        elif part.startswith("<="): op, val = "<=", part[2:].strip()
        elif part.startswith(">"):  op, val = ">",  part[1:].strip()
        elif part.startswith("<"):  op, val = "<",  part[1:].strip()
        elif part.startswith("~="): op, val = "~=", part[2:].strip()
        else:                       op, val = "==", part
        cmpv = cmp_semver(ver, val)
        okk = (op=="==" and cmpv==0) or (op==">=" and cmpv>=0) or (op=="<=" and cmpv<=0) or (op==">" and cmpv>0) or (op=="<" and cmpv<0) or (op=="~=" and (parse_semver(ver)[0]==parse_semver(val)[0] and cmpv>=0))
        if not okk: return False
    return True
    
def arch_compatible(pkg_arch: str, want_arch: str) -> bool:
    pkg_norm = (pkg_arch or "").strip().lower()
    want_norm = (want_arch or "").strip().lower()

    host_arch = (os.uname().machine if hasattr(os, "uname") else "")
    host_norm = (host_arch or "").strip().lower()

    universal_arches = {"noarch", "any", "none"}

    if pkg_norm in universal_arches:
        return True

    if want_norm in universal_arches:
        return pkg_norm == host_norm

    return pkg_norm == want_norm

# =========================== Dep grammar (AND/OR + atoms) =====================
TOK_RE = re.compile(r"\s*(\(|\)|\|\||\||,|>=|<=|==|=|>|<|~=?|\w[\w\-\._+]*)")

@dataclass(frozen=True)
class Atom:
    name: str
    op: str = ""
    ver: str = ""

@dataclass(frozen=True)
class DepExpr:
    kind: str                  # "atom" | "and" | "or"
    atom: Optional[Atom]=None
    left: Optional["DepExpr"]=None
    right: Optional["DepExpr"]=None
    @staticmethod
    def atom_(a: Atom): return DepExpr("atom", atom=a)
    @staticmethod
    def AND(a,b): return DepExpr("and", left=a, right=b)
    @staticmethod
    def OR(a,b):  return DepExpr("or",  left=a, right=b)

def parse_dep_expr(s: str) -> DepExpr:
    tokens = [t for t in TOK_RE.findall(s)]
    pos = 0
    def peek(): return tokens[pos] if pos < len(tokens) else None
    def eat(t=None):
        nonlocal pos
        tok = peek()
        if t and tok != t: raise ValueError(f"Expected {t}, got {tok}")
        pos += 1
        return tok
    def parse_atom() -> DepExpr:
        name = eat()
        if name in ("|", "||", ",", "(", ")", None):
            raise ValueError("bad dep atom")
        op = ""
        if peek() in ("==", "=", "<=", ">=", "<", ">", "~", "~="):
            op = eat()
        ver = ""
        if peek() == "(":
            eat("(")
            if peek() in ("==", "=", "<=", ">=", "<", ">", "~", "~="):
                op = eat()
                ver = eat()
                eat(")")
            else:
                value = eat()
                if value is None:
                    raise ValueError("empty dependency group")
                eat(")")
                if op:
                    ver = value
                else:
                    name = f"{name}({value})"
        if not ver and peek() in ("==", "=", "<=", ">=", "<", ">", "~", "~="):
            op = eat()
            ver = eat()
        elif op and not ver:
            ver = eat()
        return DepExpr.atom_(Atom(name=name, op=op, ver=ver))
    def parse_or() -> DepExpr:
        node = parse_atom()
        while peek() in ("|","||"):
            eat(); node = DepExpr.OR(node, parse_atom())
        return node
    def parse_and() -> DepExpr:
        node = parse_or()
        while True:
            if peek() in (",",):
                eat(","); node = DepExpr.AND(node, parse_or())
            elif peek() and peek() not in (")",):
                if peek() in ("|","||"): break
                node = DepExpr.AND(node, parse_or())
            else:
                break
        return node
    expr = parse_and()
    if pos != len(tokens): raise ValueError("junk at end of dep expr")
    return expr

def flatten_and(e: DepExpr) -> List[DepExpr]:
    if e.kind != "and":
        return [e]
    return flatten_and(e.left) + flatten_and(e.right)


def flatten_or(e: DepExpr) -> List[DepExpr]:
    if e.kind != "or":
        return [e]
    return flatten_or(e.left) + flatten_or(e.right)


def atom_to_str(atom: Optional[Atom]) -> str:
    if not atom:
        return "<invalid>"
    if atom.op and atom.ver:
        return f"{atom.name}{atom.op}{atom.ver}"
    return atom.name


def dep_expr_to_str(expr: DepExpr) -> str:
    if expr.kind == "atom":
        return atom_to_str(expr.atom)
    if expr.kind == "and":
        parts = [dep_expr_to_str(part) for part in flatten_and(expr)]
        joined = " && ".join(parts)
        return joined if len(parts) == 1 else f"({joined})"
    if expr.kind == "or":
        parts = [dep_expr_to_str(part) for part in flatten_or(expr)]
        joined = " || ".join(parts)
        return joined if len(parts) == 1 else f"({joined})"
    return "<invalid expr>"

# =========================== Package metadata =================================
@dataclass
class PkgMeta:
    name: str
    version: str
    release: str = "1"
    arch: str = "noarch"
    summary: str = ""
    url: str = ""
    license: str = ""
    developer: str = ""
    requires: List[str] = field(default_factory=list)
    build_requires: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    obsoletes: List[str] = field(default_factory=list)
    provides: List[str] = field(default_factory=list)
    provides_by_package: Dict[str, List[str]] = field(default_factory=dict)
    symbols: List[str] = field(default_factory=list)
    recommends: List[str] = field(default_factory=list)
    suggests: List[str] = field(default_factory=list)
    size: int = 0
    sha256: Optional[str] = None
    blob: Optional[str] = None
    repo: str = ""
    prio: int = 10
    # Heuristic tuning
    bias: float = 1.0
    decay: float = 0.95
    kernel: bool = False
    mkinitcpio_preset: Optional[str] = None
    deltas: List[Dict[str, Any]] = field(default_factory=list)
    @staticmethod
    def from_dict(d: dict, repo_name="(local)", prio=0, bias: float = 1.0, decay: float = 0.95) -> "PkgMeta":
        return PkgMeta(
            name=d["name"], version=d["version"], release=d.get("release","1"),
            arch=d.get("arch","noarch"), summary=d.get("summary",""), url=d.get("url",""),
            license=d.get("license",""), developer=d.get("developer",""), requires=d.get("requires",[]), conflicts=d.get("conflicts",[]),
            build_requires=d.get("build_requires", []), obsoletes=d.get("obsoletes",[]), provides=d.get("provides",[]), provides_by_package=d.get("provides_by_package", {}), symbols=d.get("symbols",[]), recommends=d.get("recommends",[]),
            suggests=d.get("suggests",[]), size=d.get("size",0), sha256=d.get("sha256"), blob=d.get("blob"),
            repo=repo_name, prio=prio, bias=bias, decay=decay, kernel=d.get("kernel", False),
            mkinitcpio_preset=d.get("mkinitcpio_preset"), deltas=d.get("deltas", []))

# =========================== Repos ============================================
@dataclass
class Repo:
    name: str
    url: str
    priority: int=10
    bias: float=1.0
    decay: float=0.95

def list_repos() -> List[Repo]:
    return [Repo(**r) for r in read_json(REPO_LIST)]

def save_repos(rs: List[Repo]):
    with operation_phase(privileged=True):
        write_json(REPO_LIST, [dataclasses.asdict(r) for r in rs])

def add_repo(name,url,priority=10,bias=1.0,decay=0.95):
    rs=list_repos()
    if any(r.name==name for r in rs): die(f"repo {name} exists")
    rs.append(Repo(name,url,priority,bias,decay)); save_repos(rs); ok(f"Added repo {name}")

def del_repo(name):
    save_repos([r for r in list_repos() if r.name!=name]); ok(f"Removed repo {name}")

def fetch_repo_index(repo: Repo) -> List[PkgMeta]:
    idx_url = repo.url.rstrip("/") + "/index.json"
    raw, _ = _resolve_lpm_attr("urlread", urlread)(idx_url)
    j = json.loads(raw.decode("utf-8"))
    return [PkgMeta.from_dict(p, repo.name, repo.priority, repo.bias, repo.decay) for p in j.get("packages",[])]

def load_universe() -> Dict[str, List[PkgMeta]]:
    out: Dict[str,List[PkgMeta]] = {}
    for repo in sorted(list_repos(), key=lambda r: r.priority):
        try:
            pkgs = fetch_repo_index(repo)
        except Exception as e:
            warn(f"repo {repo.name}: {e}"); continue
        for p in pkgs:
            if not arch_compatible(p.arch, ARCH): continue
            out.setdefault(p.name, []).append(p)
    for name, lst in out.items(): 
        lst.sort(key=lambda p: (p.prio, parse_semver(p.version)), reverse=True)
    return out

# =========================== SQLite state =====================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS installed(
  name TEXT PRIMARY KEY,
  version TEXT NOT NULL,
  release TEXT NOT NULL,
  arch TEXT NOT NULL,
  provides TEXT NOT NULL,
  symbols TEXT NOT NULL,
  requires TEXT NOT NULL,
  manifest TEXT NOT NULL,
  explicit INTEGER NOT NULL,
  install_time INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  action TEXT NOT NULL,
  name TEXT NOT NULL,
  from_ver TEXT,
  to_ver TEXT,
  details TEXT
);
CREATE TABLE IF NOT EXISTS snapshots(
  id INTEGER PRIMARY KEY,
  ts INTEGER NOT NULL,
  tag TEXT NOT NULL,
  archive TEXT NOT NULL
);
"""
_DB_PATH_OVERRIDE: Optional[Path] = None


def _state_owner_group() -> tuple[Optional[int], Optional[int]]:
    try:
        info = privilege_info()
    except Exception:
        return None, None
    if privileges_enabled() and info.privileged_uid == 0 and info.unpriv_uid != info.privileged_uid:
        return info.unpriv_uid, info.unpriv_gid
    return None, None


def _apply_state_permissions(path: Path, *, directory: bool) -> None:
    owner, group = _state_owner_group()
    mode = 0o775 if directory else 0o664
    uid = owner if owner is not None else -1
    gid = group if group is not None else -1
    with privileged_section():
        try:
            if owner is not None or group is not None:
                os.chown(path, uid, gid)
        except OSError:
            pass
        try:
            os.chmod(path, mode)
        except OSError:
            pass


def _open_state_db(path: Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _apply_state_permissions(path.parent, directory=True)
    c = sqlite3.connect(str(path))
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    cols = [r[1] for r in c.execute("PRAGMA table_info(installed)")]
    if "symbols" not in cols:
        c.execute("ALTER TABLE installed ADD COLUMN symbols TEXT NOT NULL DEFAULT '[]'")
    if "requires" not in cols:
        c.execute("ALTER TABLE installed ADD COLUMN requires TEXT NOT NULL DEFAULT '[]'")
    if "explicit" not in cols:
        c.execute("ALTER TABLE installed ADD COLUMN explicit INTEGER NOT NULL DEFAULT 0")
    c.commit()
    _apply_state_permissions(path, directory=False)
    return c


def db() -> sqlite3.Connection:
    override = _resolve_lpm_attr("db", None)
    if override is not None and override is not db:
        return override()
    state_dir_override = os.environ.get("LPM_STATE_DIR")
    if state_dir_override:
        path = Path(state_dir_override) / "state.db"
    else:
        path = _DB_PATH_OVERRIDE or DB_PATH
    return _open_state_db(path)

def db_installed(conn) -> Dict[str,dict]:
    override = _resolve_lpm_attr("db_installed", None)
    if override is not None and override is not db_installed:
        return override(conn)
    res = {}
    rows = conn.execute(
        "SELECT name,version,release,arch,provides,symbols,requires,manifest,explicit FROM installed"
    )
    for r in rows:
        res[r[0]] = {
            "version": r[1],
            "release": r[2],
            "arch": r[3],
            "provides": json.loads(r[4]),
            "symbols": json.loads(r[5]) if r[5] else [],
            "requires": json.loads(r[6]) if r[6] else [],
            "manifest": json.loads(r[7]),
            "explicit": bool(r[8]),
        }
    return res

# =========================== Snapshots =====================================
def create_snapshot(tag: str, files: Iterable[Path]) -> str:
    ts = int(time.time())
    safe_tag = re.sub(r"[^A-Za-z0-9._-]", "_", tag)
    archive = SNAPSHOT_DIR / f"{ts}-{safe_tag}.tar.zst"
    cctx = zstd.ZstdCompressor()
    with operation_phase(privileged=True):
        with atomic_replace(archive, mode=0o644, open_mode="wb") as fh:
            with cctx.stream_writer(fh) as compressor:
                with tarfile.open(fileobj=compressor, mode="w|") as tf:
                    for p in files:
                        p = Path(p)
                        if not p.exists():
                            continue
                        arcname = p.as_posix().lstrip("/")
                        tf.add(str(p), arcname=arcname)
    conn = db()
    conn.execute("INSERT INTO snapshots(ts, tag, archive) VALUES(?,?,?)", (ts, tag, str(archive)))
    conn.commit()
    conn.close()
    prune_snapshots(MAX_SNAPSHOTS)
    return str(archive)


def restore_snapshot(archive: Path) -> None:
    archive = Path(archive)
    dctx = zstd.ZstdDecompressor()
    with archive.open("rb") as f:
        with dctx.stream_reader(f) as reader:
            with tarfile.open(fileobj=reader, mode="r|") as tf:
                for m in tf:
                    dest = Path("/") / m.name
                    if dest.exists():
                        if dest.is_dir():
                            if not m.isdir():
                                shutil.rmtree(dest)
                        else:
                            dest.unlink()
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tf.extract(m, path="/", filter="data")

def prune_snapshots(limit: int = MAX_SNAPSHOTS) -> None:
    if limit <= 0:
        return
    conn = db()
    rows = list(conn.execute("SELECT id,archive FROM snapshots ORDER BY id DESC"))
    if len(rows) <= limit:
        conn.close()
        return
    for sid, archive in rows[limit:]:
        try:
            Path(archive).unlink(missing_ok=True)
        except Exception as e:
            warn(f"rm {archive}: {e}")
        conn.execute("DELETE FROM snapshots WHERE id=?", (sid,))
    conn.commit()
    conn.close()

# =========================== Universe / Providers =============================
@dataclass
class Universe:
    candidates_by_name: Dict[str, List[PkgMeta]]
    providers: Dict[str, List[PkgMeta]]  # token -> pkgs (including name token)
    installed: Dict[str, dict]
    pins: Dict[str,str]
    holds: Set[str]


def build_universe() -> Universe:
    conn = db(); installed = db_installed(conn)
    pins = read_json(PIN_FILE)
    holds=set(pins.get("hold",[]))
    prefer: Dict[str,str] = pins.get("prefer",{})
    allpkgs = load_universe()
    providers: Dict[str,List[PkgMeta]] = {}
    def add_prov(tok: str, p: PkgMeta): providers.setdefault(tok, []).append(p)
    for name, lst in allpkgs.items():
        for p in lst:
            add_prov(p.name, p)
            for prov in p.provides:
                m = re.match(r"^([A-Za-z0-9._+\-]+)\s*(==|=|>=|<=|>|<|~=?)*\s*(.*)?$", prov.strip())
                if m:
                    nm, op, ver = m.group(1), (m.group(2) or ""), (m.group(3) or "")
                    add_prov(nm, p)
                    if op and ver: add_prov(f"{nm}{'==' if op=='=' else op}{ver}", p)
    for tok, lst in providers.items():
        lst.sort(key=lambda p: (p.prio, parse_semver(p.version)), reverse=True)
    return Universe(allpkgs, providers, installed, prefer, holds)


def register_universe_candidate(u: Universe, meta: PkgMeta) -> None:
    """Register *meta* as an available candidate in *u*.

    This mirrors the logic in :func:`build_universe` by inserting the package
    into both the ``candidates_by_name`` and ``providers`` mappings while
    preserving their priority and version ordering.
    """

    candidates = u.candidates_by_name.setdefault(meta.name, [])
    if meta not in candidates:
        candidates.append(meta)
    candidates.sort(key=lambda p: (p.prio, parse_semver(p.version)), reverse=True)

    def _add_provider_token(token: str) -> None:
        providers = u.providers.setdefault(token, [])
        if meta not in providers:
            providers.append(meta)
        providers.sort(key=lambda p: (p.prio, parse_semver(p.version)), reverse=True)

    _add_provider_token(meta.name)
    for prov in meta.provides:
        prov = (prov or "").strip()
        if not prov:
            continue
        m = re.match(r"^([A-Za-z0-9._+\-]+)\s*(==|=|>=|<=|>|<|~=?)*\s*(.*)?$", prov)
        if not m:
            continue
        nm, op, ver = m.group(1), (m.group(2) or ""), (m.group(3) or "")
        if nm:
            _add_provider_token(nm)
        if nm and op and ver:
            norm_op = "==" if op == "=" else op
            _add_provider_token(f"{nm}{norm_op}{ver}")


def _remove_universe_candidate(u: Universe, meta: PkgMeta) -> None:
    """Remove *meta* from the universe candidate/provider mappings."""

    candidates = u.candidates_by_name.get(meta.name)
    if candidates:
        u.candidates_by_name[meta.name] = [p for p in candidates if p is not meta]
        if not u.candidates_by_name[meta.name]:
            del u.candidates_by_name[meta.name]

    empty_tokens: List[str] = []
    for token, providers in u.providers.items():
        filtered = [p for p in providers if p is not meta]
        if not filtered:
            empty_tokens.append(token)
            continue
        if len(filtered) != len(providers):
            u.providers[token] = filtered
    for token in empty_tokens:
        u.providers.pop(token, None)


def _first_missing_dependency(
    u: Universe,
    expr: DepExpr,
    installed: Mapping[str, dict],
    installed_providers: Mapping[str, Set[str]],
) -> Optional[DepExpr]:
    """Return the first dependency sub-expression without providers, if any."""

    if expr.kind == "atom":
        if providers_for(u, expr.atom):
            return None
        if _match_dep_expr_against_installed(expr, installed, installed_providers):
            return None
        return expr

    if expr.kind == "or":
        parts = flatten_or(expr)
        for part in parts:
            if _first_missing_dependency(u, part, installed, installed_providers) is None:
                return None
        for part in parts:
            missing = _first_missing_dependency(u, part, installed, installed_providers)
            if missing is not None:
                return missing
        return expr

    if expr.kind == "and":
        for part in flatten_and(expr):
            missing = _first_missing_dependency(u, part, installed, installed_providers)
            if missing is not None:
                return missing
        return None

    return expr


def _iter_requires(meta: PkgMeta, include_build_requires: bool = False) -> Iterable[str]:
    yield from meta.requires or []
    if include_build_requires:
        yield from getattr(meta, "build_requires", []) or []


def prune_universe_missing_providers(
    u: Universe, include_build_requires: bool = False
) -> Tuple[Dict[Tuple[str, str], str], Dict[str, List[str]]]:
    """Prune candidates that reference dependencies with no providers."""

    disqualified: Dict[Tuple[str, str], str] = {}
    terminal: Dict[str, List[str]] = {}
    installed_providers = _installed_provider_map(u.installed)

    changed = True
    while changed:
        changed = False
        snapshot = [
            (name, list(candidates))
            for name, candidates in u.candidates_by_name.items()
        ]
        for name, candidates in snapshot:
            for meta in candidates:
                key = (meta.name, meta.version)
                if key in disqualified:
                    continue

                reason: Optional[str] = None
                for req in _iter_requires(meta, include_build_requires):
                    if not req:
                        continue
                    try:
                        expr = parse_dep_expr(req)
                    except Exception:
                        continue
                    missing = _first_missing_dependency(
                        u, expr, u.installed, installed_providers
                    )
                    if missing is not None:
                        reason = (
                            f"No provider for dependency '{dep_expr_to_str(missing)}' "
                            f"required by {meta.name}-{meta.version}"
                        )
                        break

                if reason is None:
                    continue

                disqualified[key] = reason
                _remove_universe_candidate(u, meta)
                if not u.candidates_by_name.get(name):
                    terminal.setdefault(name, []).append(reason)
                changed = True
                break
            if changed:
                break

    return disqualified, terminal


def providers_for(u: Universe, atom: Atom) -> List[PkgMeta]:
    cands = list(u.providers.get(atom.name, []))
    if atom.op and atom.ver:
        cands = [p for p in cands if satisfies(p.version, f"{atom.op}{atom.ver}")]
    return cands

# =========================== Resolver encoding =================================
def expr_to_cnf_disj(u: Universe, e: DepExpr, cnf: CNF, var_of: Dict[Tuple[str,str],int]) -> List[int]:
    if e.kind=="atom":
        lits=[var_of[(p.name,p.version)] for p in providers_for(u, e.atom)]
        return lits
    elif e.kind=="or":
        return list(set(expr_to_cnf_disj(u, e.left, cnf, var_of) + expr_to_cnf_disj(u, e.right, cnf, var_of)))
    else:
        die("expr_to_cnf_disj called on AND unexpectedly")

def encode_resolution(
    u: Universe,
    goals: List[DepExpr],
    goal_texts: Optional[List[str]] = None,
    *,
    include_build_requires: bool = False,
) -> Tuple[
    CNF,
    Dict[Tuple[str, str], int],
    Set[int],
    Set[int],
    Dict[int, float],
    Dict[int, float],
    Dict[Tuple[str, str], str],
    Dict[str, List[str]],
]:
    disqualified, terminal = prune_universe_missing_providers(
        u, include_build_requires
    )
    installed_providers = _installed_provider_map(u.installed)

    cnf = CNF()
    var_of: Dict[Tuple[str,str],int] = {}
    bias_map: Dict[int,float] = {}
    decay_map: Dict[int,float] = {}
    for name,lst in u.candidates_by_name.items():
        for p in lst:
            v = cnf.new_var(f"{p.name}=={p.version}")
            var_of[(p.name,p.version)] = v
            bias_map[v] = p.bias
            decay_map[v] = p.decay
    # At-most-one per name
    for name, lst in u.candidates_by_name.items():
        vars_for_name = [var_of[(p.name, p.version)] for p in lst]
        n = len(vars_for_name)
        if n <= 1:
            continue
        # Sequential counter encoding (Sinz 2005) for at-most-one
        aux = [cnf.new_var(f"amo_{name}_{i}") for i in range(n - 1)]
        cnf.add([-vars_for_name[0], aux[0]])
        for i in range(1, n - 1):
            v = vars_for_name[i]
            cnf.add([-v, aux[i]])
            cnf.add([-aux[i - 1], aux[i]])
            cnf.add([-v, -aux[i - 1]])
        cnf.add([-vars_for_name[-1], -aux[-1]])

    prefer_true: Set[int]=set(); prefer_false: Set[int]=set()
    # Bias: installed, newest
    for name,lst in u.candidates_by_name.items():
        inst=u.installed.get(name)
        if inst and (name,inst["version"]) in var_of:
            prefer_true.add(var_of[(name,inst["version"])])
        if lst:
            prefer_true.add(var_of[(lst[0].name,lst[0].version)])

    # Pins: hard restrict
    for name, cons in u.pins.items():
        if name in u.candidates_by_name:
            allowed=[]
            for p in u.candidates_by_name[name]:
                if satisfies(p.version, cons): allowed.append(var_of[(p.name,p.version)])
            if allowed:
                allowed_set=set(allowed)
                for p in u.candidates_by_name[name]:
                    v=var_of[(p.name,p.version)]
                    if v not in allowed_set: cnf.add([-v])
                for v in allowed: prefer_true.add(v)
    def add_pkg_constraints(p: PkgMeta):
        vp = var_of[(p.name,p.version)]
        # requires
        for s in _iter_requires(p, include_build_requires):
            if not s:
                continue
            e = parse_dep_expr(s)
            if e.kind == "and":
                for part in flatten_and(e):
                    disj = expr_to_cnf_disj(u, part, cnf, var_of)
                    if not disj:
                        if _match_dep_expr_against_installed(part, u.installed, installed_providers):
                            continue
                        part_label = dep_expr_to_str(part)
                        raise ResolutionError(
                            f"No provider for dependency '{part_label}' required by {p.name}-{p.version}"
                        )
                    cnf.add([-vp] + disj)
            else:
                disj = expr_to_cnf_disj(u, e, cnf, var_of)
                if not disj:
                    if _match_dep_expr_against_installed(e, u.installed, installed_providers):
                        continue
                    req_label = dep_expr_to_str(e)
                    raise ResolutionError(
                        f"No provider for dependency '{req_label}' required by {p.name}-{p.version}"
                    )
                cnf.add([-vp] + disj)
        # conflicts / obsoletes
        for lst in (p.conflicts, p.obsoletes):
            for s in lst:
                if not s: continue
                e = parse_dep_expr(s)
                parts = flatten_and(e) if e.kind=="and" else [e]
                for part in parts:
                    disj = expr_to_cnf_disj(u, part, cnf, var_of)
                    for q in disj: cnf.add([-vp, -q])
        # soft deps bias
        for s in p.recommends + p.suggests:
            try: e=parse_dep_expr(s)
            except Exception: continue
            lits=[]
            if e.kind=="and":
                for part in flatten_and(e): lits += expr_to_cnf_disj(u, part, cnf, var_of)
            else:
                lits = expr_to_cnf_disj(u, e, cnf, var_of)
            for lit in lits: prefer_true.add(lit)

    for name,lst in u.candidates_by_name.items():
        for p in lst: add_pkg_constraints(p)

    # goals
    def _format_reasons(name: Optional[str]) -> Optional[str]:
        if not name:
            return None
        reasons = terminal.get(name)
        if not reasons:
            return None
        return "; ".join(dict.fromkeys(reasons))

    for idx, g in enumerate(goals):
        goal_label = None
        if goal_texts and idx < len(goal_texts):
            goal_label = goal_texts[idx]
        goal_label = goal_label or dep_expr_to_str(g)
        if g.kind=="and":
            for part in flatten_and(g):
                disj = expr_to_cnf_disj(u, part, cnf, var_of)
                if not disj:
                    if _match_dep_expr_against_installed(part, u.installed, installed_providers):
                        continue
                    part_label = dep_expr_to_str(part)
                    reason = None
                    if part.kind == "atom" and part.atom:
                        reason = _format_reasons(part.atom.name)
                    if reason:
                        raise ResolutionError(reason)
                    raise ResolutionError(
                        f"No provider for goal part '{part_label}' (from '{goal_label}')"
                    )
                cnf.add(disj)
        else:
            disj = expr_to_cnf_disj(u, g, cnf, var_of)
            if not disj:
                if _match_dep_expr_against_installed(g, u.installed, installed_providers):
                    continue
                reason = None
                if g.kind == "atom" and g.atom:
                    reason = _format_reasons(g.atom.name)
                if reason:
                    raise ResolutionError(reason)
                raise ResolutionError(f"No provider for goal '{goal_label}'")
            cnf.add(disj)

    return (
        cnf,
        var_of,
        prefer_true,
        prefer_false,
        bias_map,
        decay_map,
        disqualified,
        terminal,
    )

def solve(
    goals: List[str], universe: Universe, *, include_build_requires: bool = False
) -> List[PkgMeta]:
    def _summarize_unsat_packages(packages: List[str]) -> str:
        pkg_set = set(packages)
        conflicts: Set[Tuple[str, str]] = set()
        dep_edges: Dict[str, Set[str]] = {name: set() for name in pkg_set}

        for name in pkg_set:
            for candidate in universe.candidates_by_name.get(name, []):
                # detect conflicts/obsoletes among the unsat core packages
                for expr_text in list(candidate.conflicts) + list(candidate.obsoletes):
                    if not expr_text:
                        continue
                    try:
                        expr = parse_dep_expr(expr_text)
                    except Exception:
                        continue
                    parts = flatten_and(expr) if expr.kind == "and" else [expr]
                    for part in parts:
                        if part.kind != "atom" or not part.atom:
                            continue
                        for provider in providers_for(universe, part.atom):
                            if provider.name in pkg_set and provider.name != name:
                                pair = tuple(sorted((name, provider.name)))
                                conflicts.add(pair)

                # build dependency graph over the unsat packages
                for req in _iter_requires(candidate, include_build_requires):
                    if not req:
                        continue
                    try:
                        expr = parse_dep_expr(req)
                    except Exception:
                        continue
                    parts = flatten_and(expr) if expr.kind == "and" else [expr]
                    for part in parts:
                        if part.kind != "atom" or not part.atom:
                            continue
                        for provider in providers_for(universe, part.atom):
                            if provider.name in pkg_set and provider.name != name:
                                dep_edges[name].add(provider.name)

        cycle: List[str] = []
        visited: Set[str] = set()
        stack: List[str] = []
        onstack: Set[str] = set()

        def dfs(node: str) -> bool:
            visited.add(node)
            stack.append(node)
            onstack.add(node)
            for nxt in dep_edges.get(node, ()):
                if nxt not in visited:
                    if dfs(nxt):
                        return True
                elif nxt in onstack:
                    idx = stack.index(nxt)
                    cycle.extend(stack[idx:] + [nxt])
                    return True
            stack.pop()
            onstack.discard(node)
            return False

        for node in sorted(pkg_set):
            if node not in visited and dfs(node):
                break

        details: List[str] = []
        if conflicts:
            pairs = [f"{a} ↔ {b}" for a, b in sorted(conflicts)]
            details.append("conflicts: " + ", ".join(pairs))
        if cycle:
            details.append("dependency cycle: " + " -> ".join(cycle))
        if not details:
            return ""
        return " (" + "; ".join(details) + ")"

    goal_exprs = [parse_dep_expr(s) for s in goals]
    (
        cnf,
        var_of,
        ptrue,
        pfalse,
        bias_map,
        decay_map,
        _disqualified,
        terminal_errors,
    ) = encode_resolution(
        universe, goal_exprs, goals, include_build_requires=include_build_requires
    )

    for expr in goal_exprs:
        if expr.kind != "atom":
            continue
        reasons = terminal_errors.get(expr.atom.name)
        if reasons:
            raise ResolutionError("; ".join(dict.fromkeys(reasons)))
    var_decay = float(CONF.get("VSIDS_VAR_DECAY", "0.95"))
    cla_decay = float(CONF.get("VSIDS_CLAUSE_DECAY", "0.999"))
    solver = CDCLSolver(
        cnf,
        ptrue,
        pfalse,
        bias_map,
        decay_map,
        var_decay=var_decay,
        cla_decay=cla_decay,
        max_learnts=MAX_LEARNT_CLAUSES,
    )
    res = solver.solve([])
    inv: Dict[int,Tuple[str,str]] = {v:k for k,v in var_of.items()}
    if not res.sat:
        names = sorted({inv.get(abs(l))[0] for l in (res.unsat_core or []) if abs(l) in inv})
        # A SAT core is intentionally minimal and may contain only the package
        # where the contradiction surfaced.  Expand it through package
        # requirements before producing diagnostics so cycles and the actual
        # conflicting pair are not hidden from the administrator.
        diagnostic_names = set(names)
        pending = list(names)
        while pending:
            current = pending.pop()
            for candidate in universe.candidates_by_name.get(current, []):
                for requirement in _iter_requires(candidate, include_build_requires):
                    try:
                        expression = parse_dep_expr(requirement)
                    except Exception:
                        continue
                    parts = flatten_and(expression) if expression.kind == "and" else [expression]
                    for part in parts:
                        if part.kind != "atom" or not part.atom:
                            continue
                        for provider in providers_for(universe, part.atom):
                            if provider.name not in diagnostic_names:
                                diagnostic_names.add(provider.name)
                                pending.append(provider.name)
        details = _summarize_unsat_packages(sorted(diagnostic_names))
        raise ResolutionError(
            "Unsatisfiable dependency set involving: " + ", ".join(names) + details
        )
    chosen: Dict[str,PkgMeta] = {}
    for vid,val in res.assign.items():
        if not val: continue
        key = inv.get(vid); 
        if not key: continue
        name,ver = key
        for p in universe.candidates_by_name.get(name, []):
            if p.version==ver: chosen[name]=p; break
    # Stable dependency-first ordering.  Cycles are legal when the selected
    # packages are otherwise satisfiable, so collapse them naturally by
    # stopping recursion at the active DFS stack instead of recursing forever.
    chosen_names = set(chosen)
    ordered: List[PkgMeta] = []
    permanent: Set[str] = set()
    visiting: Set[str] = set()

    def visit(name: str) -> None:
        if name in permanent or name in visiting:
            return
        visiting.add(name)
        package = chosen[name]
        dependencies: Set[str] = set()
        for requirement in _iter_requires(package, include_build_requires):
            expression = parse_dep_expr(requirement)
            parts = flatten_and(expression) if expression.kind == "and" else [expression]
            for part in parts:
                if part.kind != "atom" or not part.atom:
                    continue
                selected = sorted(
                    provider.name
                    for provider in providers_for(universe, part.atom)
                    if provider.name in chosen_names
                )
                if selected:
                    dependencies.add(selected[0])
        for dependency in sorted(dependencies):
            visit(dependency)
        visiting.remove(name)
        permanent.add(name)
        ordered.append(package)

    for name in sorted(chosen):
        visit(name)
    return ordered

# =========================== Hooks =============================================
def _detect_python_interpreter() -> Optional[str]:
    exe = getattr(sys, "executable", None)
    if exe:
        exe_name = Path(exe).name.lower()
        if ("python" in exe_name or "pypy" in exe_name) and os.access(exe, os.X_OK):
            return exe
    for candidate in ("python3", "python", "pypy3", "pypy"):
        resolved = shutil.which(candidate)
        if resolved and os.access(resolved, os.X_OK):
            return resolved
    return None




class AppHookExecutionError(RuntimeError):
    def __init__(self, *, hook_name: str, hook_path: Path, package_context: str, reason: str):
        self.hook_name = hook_name
        self.hook_path = Path(hook_path)
        self.package_context = package_context
        self.reason = reason
        super().__init__(f"Hook {hook_name} failed for {package_context} at {hook_path}: {reason}")

def _detect_python_for_hooks() -> Optional[str]:
    return _detect_python_interpreter()


def _shebang_command(script: Path) -> Optional[List[str]]:
    try:
        with script.open("rb") as fh:
            first_line = fh.readline()
    except OSError:
        return None

    if not first_line.startswith(b"#!"):
        return None

    try:
        decoded = first_line[2:].decode("utf-8")
    except UnicodeDecodeError:
        decoded = first_line[2:].decode("latin-1")
    decoded = decoded.strip()
    if not decoded:
        return None

    return shlex.split(decoded)


def _run_hook_script(script: Path, env: Dict[str, str], *, hook_name: str, package_context: str):
    merged_env = {**os.environ, **env}
    if os.access(script, os.X_OK):
        try:
            with privileged_section():
                subprocess.run([str(script)], env=merged_env, check=True)
            return
        except OSError as exc:
            if exc.errno not in {errno.ENOEXEC, errno.EACCES}:
                raise

    if script.suffix == ".py":
        interpreter = _detect_python_for_hooks()
        if interpreter:
            with privileged_section():
                subprocess.run([interpreter, str(script)], env=merged_env, check=True)
            return

        shebang_cmd = _shebang_command(script)
        if shebang_cmd:
            with privileged_section():
                subprocess.run([*shebang_cmd, str(script)], env=merged_env, check=True)
            return

        raise RuntimeError(f"Unable to locate Python interpreter for hook {script}")

    shebang_cmd = _shebang_command(script)
    if shebang_cmd:
        with privileged_section():
            subprocess.run([*shebang_cmd, str(script)], env=merged_env, check=True)


def _run_hook_config(config_path: Path, env: Dict[str, str], *, hook_name: str, package_context: str) -> None:
    merged_env = {**os.environ, **env}
    hooks = load_hooks([config_path.parent])
    hook = hooks.get(config_path.stem)
    if hook is None:
        return
    exec_cmd = list(hook.action.exec)
    if exec_cmd:
        exec_path = Path(exec_cmd[0])
        if exec_path.is_absolute() and not exec_path.exists():
            for parent in config_path.parents:
                if parent.name == "usr":
                    root = parent.parent
                    candidate = root / exec_path.relative_to("/")
                    if candidate.exists():
                        exec_cmd[0] = str(candidate)
                    break
        exec_path = Path(exec_cmd[0])
        if exec_path.is_absolute():
            _ensure_executable(exec_path)
    with privileged_section():
        subprocess.run(exec_cmd, env=merged_env, check=True)


def run_hook(hook: str, env: Dict[str,str], *, failure_mode: str = HookFailureMode.STRICT, package_context: Optional[str] = None):
    hook_dir = _resolve_lpm_attr("HOOK_DIR", HOOK_DIR)
    liblpm_dirs = _resolve_lpm_attr("LIBLPM_HOOK_DIRS", LIBLPM_HOOK_DIRS)
    hook_dirs = [hook_dir, *(liblpm_dirs or ())]
    if hook_dir is not None:
        hook_dir = Path(hook_dir)
        if hook_dir.parts[-2:] == ("lpm", "hooks"):
            hook_dirs.append(hook_dir.parent.parent / "liblpm" / "hooks")
    seen: set[Path] = set()

    hook_names = {hook, hook.replace("_", "-")}

    failures: List[AppHookExecutionError] = []

    def _handle(exc: Exception, hook_name_local: str, hook_path_local: Path) -> None:
        err = AppHookExecutionError(hook_name=hook_name_local, hook_path=hook_path_local, package_context=package_context or env.get("LPM_PKG", "unknown-package"), reason=str(exc))
        if failure_mode == HookFailureMode.COLLECT:
            warn(str(err))
            failures.append(err)
            return
        raise err

    for base_dir in hook_dirs:
        if base_dir is None:
            continue
        path = Path(base_dir)
        if path in seen:
            continue
        seen.add(path)

        for hook_name in hook_names:
            hook_path = path / hook_name
            if hook_path.is_file():
                try:
                    _run_hook_script(hook_path, env, hook_name=hook_name, package_context=package_context or env.get("LPM_PKG", "unknown-package"))
                except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
                    _handle(exc, hook_name, hook_path)

            py_path = hook_path.with_suffix(".py")
            if py_path.is_file():
                try:
                    _run_hook_script(py_path, env, hook_name=hook_name, package_context=package_context or env.get("LPM_PKG", "unknown-package"))
                except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
                    _handle(exc, hook_name, py_path)

            hook_path_ext = hook_path.with_suffix(".hook")
            if hook_path_ext.is_file():
                try:
                    _run_hook_config(hook_path_ext, env, hook_name=hook_name, package_context=package_context or env.get("LPM_PKG", "unknown-package"))
                except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
                    _handle(exc, hook_name, hook_path_ext)

            dpath = path / f"{hook_name}.d"
            if dpath.is_dir():
                for script in sorted(dpath.iterdir()):
                    if script.is_file():
                        try:
                            _run_hook_script(script, env, hook_name=hook_name, package_context=package_context or env.get("LPM_PKG", "unknown-package"))
                        except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
                            _handle(exc, hook_name, script)
        
# =========================== Service File Handling =============================
def _is_default_root(root: Path) -> bool:
    """Return True if ``root`` points at the host root filesystem."""

    root_path = Path(root)
    try:
        root_resolved = root_path.resolve(strict=False)
    except RuntimeError:
        root_resolved = root_path

    candidates = []
    for candidate in {DEFAULT_ROOT, "/"}:
        candidate_path = Path(candidate)
        try:
            candidates.append(candidate_path.resolve(strict=False))
        except RuntimeError:
            candidates.append(candidate_path)

    return any(root_resolved == candidate for candidate in candidates)


def _require_privileged_default_root(root: Path, action: str, target: str) -> None:
    if _is_default_root(root) and os.geteuid() != 0 and not privileges_enabled():
        die(f"{action} requires root privileges when {target} the default root")


@contextlib.contextmanager
def _privileged_default_root_mutation(root: Path, dry_run: bool):
    if not dry_run and _is_default_root(root) and privileges_enabled():
        with privileged_section():
            yield
    else:
        yield


SYSTEMD_UNIT_GLOB_PATTERNS = [
    "*.service",
    "*.socket",
    "*.timer",
    "*.path",
    "*.target",
    "*.mount",
    "*.automount",
    "*.swap",
    "*.device",
    "*.slice",
    "*.scope",
    "*.network",
    "*.netdev",
    "*.link",
]

SYSTEMD_UNIT_DIRECTORIES = (
    "usr/lib/systemd/system",
    "lib/systemd/system",
)

DEFAULT_INIT_SYSTEMD_DENYLIST = {"systemd", "systemd-libs"}


def _normalize_manifest_paths(manifest_entries: Optional[List[object]]) -> List[str]:
    paths: List[str] = []
    if not manifest_entries:
        return paths
    for entry in manifest_entries:
        if isinstance(entry, dict):
            path = entry.get("path")
        else:
            path = entry
        if isinstance(path, str):
            paths.append(path)
    return paths


def _iter_systemd_units_from_manifest(paths: Iterable[str]) -> Iterable[Tuple[str, str]]:
    for path in paths:
        if not isinstance(path, str):
            continue
        rel = path.lstrip("/")
        for service_dir in SYSTEMD_UNIT_DIRECTORIES:
            prefix = f"{service_dir}/"
            if rel.startswith(prefix):
                unit_name = Path(rel).name
                if any(fnmatch.fnmatch(unit_name, pattern) for pattern in SYSTEMD_UNIT_GLOB_PATTERNS):
                    yield service_dir, unit_name
                break


def _is_core_init_package(pkg_name: str) -> bool:
    denylist_raw = CONF.get("INIT_SYSTEMD_DENYLIST", "")
    denylisted_pkgs = {
        pkg.strip().lower()
        for pkg in {"", *DEFAULT_INIT_SYSTEMD_DENYLIST, *denylist_raw.split(",")}
        if pkg and pkg.strip()
    }
    return pkg_name.strip().lower() in denylisted_pkgs


def handle_service_files(pkg_name: str, root: Path, manifest_entries: Optional[List[object]] = None):
    """
    Detect service files from installed package and register them
    according to the active init system.
    """
    if _is_core_init_package(pkg_name):
        log(
            f"[init] Skipping automatic unit management for core init package '{pkg_name}'"
        )
        return

    init = detect_init_system()
    policy = CONF.get("INIT_POLICY", "manual").lower()  # auto/manual/none

    if policy == "none":
        return

    if init == "systemd":
        manage_systemd = _is_default_root(root)
        manifest_paths = _normalize_manifest_paths(manifest_entries)
        unique_units: Dict[str, Path] = {}

        for service_dir, unit_name in _iter_systemd_units_from_manifest(manifest_paths):
            svc_path = root / service_dir / unit_name
            if svc_path.is_file():
                unique_units.setdefault(unit_name, svc_path)

        if unique_units:
            units_list = ", ".join(unique_units.keys())
            if policy == "auto":
                if manage_systemd:
                    activation_note = "activation will follow automatically."
                else:
                    activation_note = "activation will follow on the target system."
            else:
                activation_note = "activation requires manual steps."
            log(
                f"[ Systemd Service Handler ] detected units {units_list}; {activation_note}"
            )

        if policy == "auto":
            if manage_systemd:
                if unique_units:
                    log(
                        "[ Systemd Service Handler ] activating detected units via systemctl enable --now"
                    )
                for unit_name in unique_units:
                    subprocess.run(["systemctl", "enable", "--now", unit_name], check=False)
            elif unique_units:
                log(
                    f"[systemd] Skipping systemctl enable for non-default root {root}; "
                    "deferring init integration"
                )

    elif init == "sysv":
        initd = root / "etc/init.d"
        if initd.exists():
            for svc in initd.iterdir():
                if policy == "auto":
                    subprocess.run(["update-rc.d", svc.name, "defaults"],
                                   check=False)
                log(f"[sysv] Found init script: {svc.name}")

    elif init == "openrc":
        initd = root / "etc/init.d"
        if initd.exists():
            for svc in initd.iterdir():
                if policy == "auto":
                    subprocess.run(["rc-update", "add", svc.name, "default"],
                                   check=False)
                log(f"[openrc] Found OpenRC service: {svc.name}")

    elif init == "runit":
        svdir = root / "etc/sv"
        runsvdir = Path("/etc/runit/runsvdir/default")
        if svdir.exists():
            for svc in svdir.iterdir():
                if policy == "auto":
                    runsvdir.mkdir(parents=True, exist_ok=True)
                    target = runsvdir / svc.name
                    try:
                        if not target.exists():
                            target.symlink_to(svc)
                    except Exception as e:
                        warn(f"runit symlink failed for {svc}: {e}")
                log(f"[runit] Found runit service: {svc.name}")

    else:
        warn("No supported init system detected")
        
        
def _load_manifest_for_package(pkg_name: str) -> List[object]:
    try:
        conn = db()
    except Exception:
        return []
    try:
        row = conn.execute("SELECT manifest FROM installed WHERE name=?", (pkg_name,)).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return []
    try:
        return json.loads(row[0])
    except Exception:
        return []


def remove_service_files(pkg_name: str, root: Path, manifest_entries: Optional[List[object]] = None):
    """
    Handle service cleanup on package removal.
    """
    if _is_core_init_package(pkg_name):
        log(
            f"[init] Skipping automatic unit management for core init package '{pkg_name}'"
        )
        return

    init = detect_init_system()
    policy = CONF.get("INIT_POLICY", "manual").lower()

    if policy == "none":
        return

    if init == "systemd":
        manage_systemd = _is_default_root(root)
        if manifest_entries is None:
            manifest_entries = _load_manifest_for_package(pkg_name)
        manifest_paths = _normalize_manifest_paths(manifest_entries)
        unique_units: Dict[str, str] = {}

        for service_dir, unit_name in _iter_systemd_units_from_manifest(manifest_paths):
            log(f"[systemd] Disabled unit ({root / service_dir}): {unit_name}")
            unique_units.setdefault(unit_name, service_dir)

        if policy == "auto":
            if manage_systemd:
                for unit_name in unique_units:
                    subprocess.run(["systemctl", "disable", "--now", unit_name], check=False)
            elif unique_units:
                log(
                    f"[systemd] Skipping systemctl disable for non-default root {root}; "
                    "deferring init integration"
                )

    elif init == "sysv":
        initd = root / "etc/init.d"
        if initd.exists():
            for svc in initd.iterdir():
                if policy == "auto":
                    subprocess.run(["update-rc.d", "-f", svc.name, "remove"],
                                   check=False)
                log(f"[sysv] Removed init script: {svc.name}")

    elif init == "openrc":
        initd = root / "etc/init.d"
        if initd.exists():
            for svc in initd.iterdir():
                if policy == "auto":
                    subprocess.run(["rc-update", "del", svc.name, "default"],
                                   check=False)
                log(f"[openrc] Removed OpenRC service: {svc.name}")

    elif init == "runit":
        runsvdir = Path("/etc/runit/runsvdir/default")
        if runsvdir.exists():
            for svc in runsvdir.iterdir():
                try:
                    if svc.is_symlink() and svc.exists():
                        svc.unlink()
                        log(f"[runit] Unlinked runit service: {svc.name}")
                except Exception as e:
                    warn(f"runit cleanup failed for {svc}: {e}")


# =========================== Packaging helpers (.zst) ==========================
def sha256sum(p: Path) -> str:
    h=hashlib.sha256()
    with p.open("rb") as f:
        for c in iter(lambda: f.read(1<<20), b""): h.update(c)
    return h.hexdigest()

def _extract_symbols(p: Path) -> List[str]:
    try:
        with p.open("rb") as f:
            if f.read(4) != b"\x7fELF":
                return []
        res = subprocess.run(
            ["nm", "-D", "--defined-only", str(p)],
            capture_output=True,
            text=True,
            check=False,
        )
        syms = []
        for line in res.stdout.splitlines():
            parts = line.strip().split()
            if len(parts) >= 3:
                sym = parts[-1]
                if not sym.startswith("_"):
                    syms.append(sym)
        return sorted(set(syms))
    except Exception:
        return []

def _should_extract_symbols(path: Path, st: os.stat_result) -> bool:
    if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
        return False
    if st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        return True
    name = path.name
    if name.endswith(".so") or ".so." in name:
        return True
    return False


def collect_manifest(stagedir: Path) -> List[Dict[str, object]]:
    stagedir = stagedir.resolve()
    mani: List[Dict[str, object]] = []
    skip = {".lpm-meta.json", ".lpm-manifest.json"}

    for root, dirs, files in os.walk(stagedir):
        root_path = Path(root)
        # os.walk reports symlinks to directories in ``dirs`` even though it
        # does not descend into them. Include them in the package manifest.
        symlink_dirs = [name for name in dirs if (root_path / name).is_symlink()]
        dirs[:] = [name for name in dirs if name not in symlink_dirs]
        for fn in [*files, *symlink_dirs]:
            if fn in skip:
                continue
            f = root_path / fn
            try:
                st = f.lstat()
            except FileNotFoundError:
                continue

            try:
                rel = f.relative_to(stagedir).as_posix()
            except ValueError:
                rel = os.path.relpath(f, stagedir).replace(os.sep, "/")
            entry: Dict[str, object] = {"path": "/" + rel}

            if stat.S_ISLNK(st.st_mode):
                try:
                    target = os.readlink(f)
                except OSError:
                    continue
                entry["link"] = target
                entry["sha256"] = hashlib.sha256(target.encode()).hexdigest()
                entry["size"] = st.st_size
                mani.append(entry)
                continue

            entry["size"] = st.st_size
            try:
                entry["sha256"] = sha256sum(f)
            except OSError:
                entry["sha256"] = ""
            else:
                if _should_extract_symbols(f, st):
                    syms = _extract_symbols(f)
                    if syms:
                        entry["symbols"] = syms

            mani.append(entry)

    return sorted(mani, key=lambda e: e["path"])


def _normalize_metadata_text(value: Optional[str]) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    return " ".join(text.split())


def _python_package_name(dist_name: str) -> str:
    canonical = canonicalize_name(dist_name or "")
    normalized = canonical.replace(".", "-")
    if not normalized:
        die("pip build: package metadata missing name")
    if normalized.startswith("python-"):
        return normalized
    return f"python-{normalized}"


def _format_specifier(spec: Specifier) -> Optional[str]:
    op = spec.operator
    version = spec.version
    if not op or not version:
        return None
    if op == "!==" or op == "!=":
        return None
    if op == "===":
        op = "=="
    if version.endswith(".*"):
        version = version[:-2]
        if op in {"==", "="}:
            op = "~="
    if op == "=":
        op = "=="
    if not version:
        return None
    return f"{op}{version}"


def _specifier_parts(spec_set: SpecifierSet) -> List[str]:
    parts: List[str] = []
    for spec in spec_set:
        formatted = _format_specifier(spec)
        if formatted:
            parts.append(formatted)
    return parts


def _requires_python_to_deps(spec_text: Optional[str]) -> List[str]:
    if not spec_text:
        return ["python"]
    try:
        spec_set = SpecifierSet(spec_text)
    except Exception:
        return ["python"]
    parts = _specifier_parts(spec_set)
    if not parts:
        return ["python"]
    dep = "python" + parts[0]
    for extra in parts[1:]:
        dep += f", {extra}"
    return [dep]


def _requirements_from_requires_dist(entries: Iterable[str]) -> List[str]:
    env = default_environment()
    env.setdefault("extra", "")
    deps: List[str] = []
    for raw in entries:
        if raw is None:
            continue
        try:
            requirement = Requirement(str(raw))
        except Exception:
            continue
        if requirement.marker and not requirement.marker.evaluate(env):
            continue
        if requirement.extras:
            continue
        name = _python_package_name(requirement.name)
        parts = _specifier_parts(requirement.specifier)
        if parts:
            dep = name + parts[0]
            for extra in parts[1:]:
                dep += f", {extra}"
        else:
            dep = name
        deps.append(dep)
    return deps


def _detect_python_package_arch(stagedir: Path) -> str:
    for path in stagedir.rglob("*"):
        if not path.is_file():
            continue
        name = path.name
        if name.endswith((".so", ".pyd", ".dll", ".dylib")) or ".so." in name:
            return ARCH or (os.uname().machine if hasattr(os, "uname") else "") or "noarch"
    return "noarch"


def _collect_python_package_metadata(
    stagedir: Path,
    *,
    include_requires_dist: bool,
    arch_hint: Optional[str] = None,
) -> Dict[str, object]:
    metadata_paths = sorted(stagedir.rglob("*.dist-info/METADATA"))
    parser = Parser()
    chosen: Optional[Tuple[Path, object]] = None
    for meta_path in metadata_paths:
        try:
            message = parser.parsestr(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = _normalize_metadata_text(message.get("Name"))
        version = _normalize_metadata_text(message.get("Version"))
        if name and version:
            chosen = (meta_path, message)
            break
    if not chosen:
        die("pip build: unable to locate package metadata after installation")

    _path, message = chosen
    dist_name = _normalize_metadata_text(message.get("Name"))
    version = _normalize_metadata_text(message.get("Version"))
    if not dist_name or not version:
        die("pip build: package metadata missing name/version")

    pkg_name = _python_package_name(dist_name)
    summary = _normalize_metadata_text(message.get("Summary"))
    home = _normalize_metadata_text(message.get("Home-page"))
    license_ = _normalize_metadata_text(message.get("License"))

    requires = _requires_python_to_deps(message.get("Requires-Python"))
    if include_requires_dist:
        requires.extend(_requirements_from_requires_dist(message.get_all("Requires-Dist") or []))

    requires = list(dict.fromkeys(req for req in requires if req))
    arch = arch_hint or _detect_python_package_arch(stagedir)
    provides: List[str] = []
    canonical = canonicalize_name(dist_name)
    if canonical:
        provides.append(f"pypi({canonical})")

    return {
        "name": pkg_name,
        "version": version,
        "summary": summary,
        "url": home,
        "license": license_,
        "requires": requires,
        "arch": arch,
        "provides": provides,
    }


def _select_downloaded_sdist(download_dir: Path) -> Path:
    candidates = []
    for path in sorted(download_dir.iterdir()):
        if not path.is_file():
            continue
        name = path.name.lower()
        if name.endswith(".whl"):
            continue
        if name.endswith((".tar.gz", ".tar.bz2", ".tar.xz", ".tgz", ".zip")):
            candidates.append(path)
    if not candidates:
        die("pip build: unable to locate source distribution (sdist) in download directory")
    return candidates[0]


def build_python_package_from_pip(
    spec: str,
    outdir: Path,
    *,
    include_deps: bool,
    cpu_overrides: Optional[CpuOverrides] = None,
) -> Tuple[Path, PkgMeta, float]:
    start = time.time()
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="lpm-pip-") as tmp:
        stagedir = Path(tmp) / "root"
        download_dir = Path(tmp) / "download"

        try:
            requirement = Requirement(spec)
        except Exception as exc:
            raise RuntimeError(f"pip build: invalid requirement '{spec}': {exc}") from exc

        canonical_name = canonicalize_name(requirement.name or "")

        def _format_requirement(name: str) -> str:
            extras = ""
            if requirement.extras:
                extras = f"[{','.join(sorted(requirement.extras))}]"
            specifier = str(requirement.specifier)
            marker = f"; {requirement.marker}" if requirement.marker else ""
            return f"{name}{extras}{specifier}{marker}"

        attempt_specs: List[str] = []
        seen_specs: Set[str] = set()

        normalized_name = canonical_name or (requirement.name or "")
        if normalized_name:
            primary_spec = _format_requirement(normalized_name)
            if primary_spec and primary_spec not in seen_specs:
                attempt_specs.append(primary_spec)
                seen_specs.add(primary_spec)

        if canonical_name.startswith("python-"):
            trimmed_name = canonical_name[len("python-") :]
            if trimmed_name:
                trimmed_spec = _format_requirement(trimmed_name)
                if trimmed_spec and trimmed_spec not in seen_specs:
                    attempt_specs.append(trimmed_spec)
                    seen_specs.add(trimmed_spec)

        if not attempt_specs:
            attempt_specs.append(spec)

        def _prepare_dirs() -> None:
            if stagedir.exists():
                shutil.rmtree(stagedir)
            stagedir.mkdir(parents=True, exist_ok=True)
            if download_dir.exists():
                shutil.rmtree(download_dir)
            download_dir.mkdir(parents=True, exist_ok=True)

        interpreter = _detect_python_interpreter()
        if not interpreter:
            raise RuntimeError("pip build: unable to locate Python interpreter for pip execution")

        overrides = cpu_overrides.normalized() if cpu_overrides else None
        default_march = MARCH or "generic"
        default_mtune = MTUNE or "generic"
        march_base = overrides.march if overrides and overrides.march else default_march
        mtune_base = overrides.mtune if overrides and overrides.mtune else default_mtune
        march_value = (march_base or "").strip() or default_march
        mtune_value = (mtune_base or "").strip() or default_mtune

        env = os.environ.copy()
        env.setdefault("PYTHONNOUSERSITE", "1")
        base_parts = [OPT_LEVEL]
        if ENABLE_CPU_OPTIMIZATIONS:
            base_parts.extend([f"-march={march_value}", f"-mtune={mtune_value}"])
        base_parts.extend(["-pipe", "-fPIC"])
        base_flags = " ".join(part for part in base_parts if part).strip()
        extra_cflags = env.get("CFLAGS", "").strip()
        if base_flags:
            combined_flags = " ".join(filter(None, [base_flags, extra_cflags])).strip()
        else:
            combined_flags = extra_cflags
        if combined_flags:
            env["CFLAGS"] = combined_flags
            env["CXXFLAGS"] = combined_flags
        else:
            env.pop("CFLAGS", None)
            env.pop("CXXFLAGS", None)
        env["LDFLAGS"] = OPT_LEVEL
        arch_hint = overrides.arch if overrides and overrides.arch else None
        if arch_hint:
            env["ARCH"] = arch_hint
            env["LPM_ARCH"] = arch_hint
        else:
            env.setdefault("ARCH", ARCH)
            env.setdefault("LPM_ARCH", ARCH)
        if ENABLE_CPU_OPTIMIZATIONS:
            env["LPM_CPU_MARCH"] = march_value
            env["LPM_CPU_MTUNE"] = mtune_value
        else:
            env.pop("LPM_CPU_MARCH", None)
            env.pop("LPM_CPU_MTUNE", None)

        last_error: Optional[Exception] = None

        for attempt_spec in attempt_specs:
            _prepare_dirs()
            download_cmd = [
                interpreter,
                "-m",
                "pip",
                "download",
                attempt_spec,
                "--no-deps",
                "--no-binary",
                ":all:",
                "--dest",
                str(download_dir),
                "--progress-bar",
                "off",
                "--disable-pip-version-check",
            ]
            log(f"[pip] downloading {attempt_spec} source distribution")
            try:
                subprocess.run(download_cmd, check=True, env=env)
            except subprocess.CalledProcessError as exc:
                last_error = exc
                continue

            sdist_path = _select_downloaded_sdist(download_dir)

            pip_cmd = [
                interpreter,
                "-m",
                "pip",
                "install",
                str(sdist_path),
                "--no-deps",
                "--prefix",
                "/usr",
                "--root",
                str(stagedir),
                "--no-compile",
                "--disable-pip-version-check",
                "--no-warn-script-location",
                "--progress-bar",
                "off",
                "--ignore-installed",
            ]
            log(f"[pip] building from sdist {sdist_path.name} into staging root {stagedir}")
            try:
                subprocess.run(pip_cmd, check=True, env=env)
            except subprocess.CalledProcessError as exc:
                last_error = exc
                continue

            info = _collect_python_package_metadata(
                stagedir,
                include_requires_dist=include_deps,
                arch_hint=arch_hint,
            )
            meta = PkgMeta(
                name=info["name"],
                version=info["version"],
                release="1",
                arch=info["arch"],
                summary=info["summary"],
                url=info["url"],
                license=info["license"],
                requires=info["requires"],
                provides=info["provides"],
            )

            out = outdir / f"{meta.name}-{meta.version}-{meta.release}.{meta.arch}{EXT}"
            _resolve_lpm_attr("build_package", build_package)(stagedir, meta, out, sign=True)

            if maintainer_mode.is_enabled():
                try:
                    maint_result = maintainer_mode.handle_lpmbuild(
                        primary=(meta, out),
                        split_records=(),
                        script_path=None,
                        source_tree=download_dir if download_dir.exists() else None,
                    )
                except Exception as exc:
                    warn(f"Maintainer mode error: {exc}")
                else:
                    maintainer_mode.generate_indexes(maint_result, gen_index)
                    maintainer_mode.finalize_git(maint_result)

            duration = time.time() - start
            return out, meta, duration

    if last_error:
        raise last_error
    raise RuntimeError(f"pip build: unable to download or build requirement '{spec}'")


# =========================== Unified package tar opener =========================
_ZSTD_ERROR_HELP = (
    "Unable to read Zstandard-compressed package. Install the 'zstandard' "
    "Python module to handle .zst files."
)


class _ZstdStreamReaderWrapper(io.RawIOBase):
    """Ensure the underlying file handle closes with the zstd reader."""

    def __init__(self, reader: io.BufferedIOBase, fh: BinaryIO):
        self._reader = reader
        self._fh = fh

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        return self._reader.read(size)

    def close(self) -> None:
        try:
            close = getattr(self._reader, "close", None)
            if close is not None:
                close()
        finally:
            self._fh.close()
        super().close()

    def __getattr__(self, item):
        return getattr(self._reader, item)

    def __enter__(self):
        if hasattr(self._reader, "__enter__"):
            self._reader.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        if hasattr(self._reader, "__exit__"):
            self._reader.__exit__(exc_type, exc, tb)
        self.close()
        return None


def open_package_tar(blob: Path, stream: bool = True) -> tarfile.TarFile:
    """
    Open a .zst (tar+zstd) package safely using the zstandard library.
    Supports both streaming (no size header) and buffered random access.
    """
    if not blob.exists():
        die(f"Package not found: {blob}")

    if blob.suffix != EXT:
        die(f"{blob} is not a {EXT} archive")

    # Validate Zstd magic
    with blob.open("rb") as f:
        magic = f.read(4)
    if magic != b"\x28\xb5\x2f\xfd":
        die(f"{blob} is not a valid {EXT} package (bad magic header)")

    if stream:
        # Stream directly into tarfile (used for extraction)
        f = blob.open("rb")
        dctx = zstd.ZstdDecompressor()
        reader: io.BufferedIOBase = _ZstdStreamReaderWrapper(dctx.stream_reader(f), f)
        try:
            return tarfile.open(fileobj=reader, mode="r|")
        except (tarfile.ReadError, zstd.ZstdError):
            reader.close()
            die(_ZSTD_ERROR_HELP)
    else:
        # Buffer the decompression into memory for random-access tarfile
        f = blob.open("rb")
        dctx = zstd.ZstdDecompressor()
        reader = dctx.stream_reader(f)
        buf = io.BytesIO()
        try:
            while True:
                chunk = reader.read(IO_BUFFER_SIZE)
                if not chunk:
                    break
                buf.write(chunk)
        finally:
            reader.close()
            f.close()
        buf.seek(0)
        try:
            return tarfile.open(fileobj=buf, mode="r:")
        except (tarfile.ReadError, zstd.ZstdError):
            die(_ZSTD_ERROR_HELP)


# =============== BUILDPKG Function ==============================
def build_package(stagedir: Path, meta: PkgMeta, out: Path, sign=True):
    stagedir = stagedir.resolve()
    if not stagedir.is_dir():
        die(f"Stagedir {stagedir} missing")

    if not out.name.endswith(".zst"):
        out = out.with_suffix(".zst")

    use_fallback = shutil.which("zstd") is None
    if use_fallback:
        warn("zstd not found in PATH, using Python zstandard library")

    # Collect manifest including exported symbols
    mani = collect_manifest(stagedir)
    meta.symbols = sorted({s for e in mani for s in e.get("symbols", [])})

    # Write metadata + manifest *into stagedir*
    meta_path = stagedir / ".lpm-meta.json"
    mani_path = stagedir / ".lpm-manifest.json"
    meta_dict = dataclasses.asdict(meta)

    safe_write(
        meta_path,
        json.dumps(meta_dict, ensure_ascii=False, indent=2, sort_keys=True),
        mode=0o644,
    )
    safe_write(
        mani_path,
        json.dumps(mani, ensure_ascii=False, indent=2),
        mode=0o644,
    )

    # Package with tar + zstd (with Python fallback)
    if use_fallback:
        tmp_tar = out.with_suffix(".tar")
        subprocess.run(
            [
                "tar",
                "-cf", str(tmp_tar),
                "--sort=name",
                "--mtime=@0",
                "--owner=0",
                "--group=0",
                "--numeric-owner",
                "-C", str(stagedir),
                ".",
            ],
            check=True,
        )
        with tmp_tar.open("rb") as fi, out.open("wb") as fo:
            cctx = zstd.ZstdCompressor()
            with cctx.stream_writer(fo) as compressor:
                shutil.copyfileobj(fi, compressor)
        tmp_tar.unlink(missing_ok=True)
    else:
        subprocess.run(
            [
                "tar",
                "--zstd",
                "-cf", str(out),
                "--sort=name",
                "--mtime=@0",
                "--owner=0",
                "--group=0",
                "--numeric-owner",
                "-C", str(stagedir),
                ".",
            ],
            check=True,
        )

    # Sign package if signing key exists
    if sign:
        if not SIGN_KEY.exists():
            warn(f"Signing requested but key not found: {SIGN_KEY}")
        elif not os.access(SIGN_KEY, os.R_OK):
            warn(f"Signing key not readable ({SIGN_KEY}); skipping signature")
        else:
            sig = out.with_suffix(out.suffix + ".sig")
            try:
                subprocess.run(
                    [
                        "openssl",
                        "dgst",
                        "-sha256",
                        "-sign",
                        str(SIGN_KEY),
                        "-out",
                        str(sig),
                        str(out),
                    ],
                    check=True,
                )
            except subprocess.CalledProcessError as exc:
                warn(
                    "openssl failed to sign package; package will remain unsigned. "
                    f"(exit status {exc.returncode})"
                )

    ok(f"Built {out}")

# ==================================================================================
def read_package_meta(blob: Path) -> Tuple[PkgMeta, List[dict]]:
    if not str(blob).endswith(EXT):
        warn(f"{blob.name}: not a {EXT} file, attempting anyway")

    meta = None
    mani = None
    with open_package_tar(blob, stream=False) as tf:
        for m in tf.getmembers():
            name = Path(m.name).name  # normalize (handles './.lpm-meta.json')
            if name == ".lpm-meta.json":
                with tf.extractfile(m) as f:
                    meta = PkgMeta.from_dict(json.load(f))
            elif name == ".lpm-manifest.json":
                with tf.extractfile(m) as f:
                    mani = json.load(f)

    if not meta:
        die(f"{blob.name}: missing .lpm-meta.json (corrupt package)")
    if not mani:
        die(f"{blob.name}: missing .lpm-manifest.json (corrupt package)")

    return meta, mani


  
# =========================== Signature verification ===========================
def _verify_with_key(pubkey: Path, blob: Path, sig: Path) -> bool:
    try:
        subprocess.run(
            ["openssl","dgst","-sha256","-verify",str(pubkey),"-signature",str(sig),str(blob)],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return True
    except subprocess.CalledProcessError:
        return False

def verify_signature(blob: Path, sig: Optional[Path]) -> None:
    if not sig or not sig.exists():
        raise RuntimeError(f"Missing signature for {blob.name}")
    keys = sorted(TRUST_DIR.glob("*.pem")) if TRUST_DIR.exists() else []
    if not keys:
        raise RuntimeError(f"No trusted public keys in {TRUST_DIR}")
    for k in keys:
        if _verify_with_key(k, blob, sig):
            ok(f"Signature OK ({k.name}) for {blob.name}")
            return
    raise RuntimeError(f"Signature verification failed for {blob.name}")

# =========================== Install/Remove/Upgrade ===========================
def extract_tar(blob: Path, root: Path) -> List[str]:
    """
    Extract a .zst package into root using streaming mode.
    Returns the list of installed file paths.
    """
    manifest = []
    with open_package_tar(blob, stream=True) as tf:
        for m in progress_bar(tf, desc=f"Extracting {blob.name}", unit="file"):
            if Path(m.name).name in (".lpm-meta.json", ".lpm-manifest.json"):
                continue
            rel = Path(m.name).as_posix().lstrip("/")
            dest = root / rel
            if m.isdir():
                dest.mkdir(parents=True, exist_ok=True)
        …17618 tokens truncated…:
                    tf.extract(member, path=str(dest), filter="data")
            finally:
                if tf is not None:
                    tf.close()
                if fileobj is not None:
                    fileobj.close()

        try:
            _extract_with_tarfile(archive_path, target_dir)
        except Exception:
            try:
                subprocess.run(
                    [
                        "tar",
                        "--strip-components=1",
                        "-xaf",
                        str(archive_path),
                        "-C",
                        str(target_dir),
                    ],
                    check=True,
                )
            except (FileNotFoundError, subprocess.CalledProcessError):
                pass

    run_hook(
        "post_source_fetch",
        {
            "LPM_NAME": name,
            "LPM_VERSION": version,
            "LPM_RELEASE": release,
            "LPM_SRCROOT": str(srcroot),
            "LPM_SOURCE_ENTRIES": "\n".join(staged_entries),
        },
    )

    # --- Run build functions inside sandbox ---
    def run_func(func: str, cwd: Path):
        phase_aliases: Tuple[str, ...] = ()
        if func == "staging":
            phase_aliases = ("install",)
        _resolve_lpm_attr("sandboxed_run", sandboxed_run)(
            func,
            cwd,
            env,
            script_path,
            stagedir,
            buildroot,
            srcroot,
            aliases=phase_aliases,
        )

    phases = ("prepare", "build", "staging")
    with progress_bar(
        phases,
        unit="phase",
        mode="ninja",
        leave=False,
        track=True,
    ) as pbar:
        for phase in pbar:
            pbar.set_description(phase)
            try:
                run_func(phase, srcroot)
            except subprocess.CalledProcessError as e:
                die(f"{script.name}: function '{phase}' failed with code {e.returncode}")
    phase_count = getattr(pbar, "completed", pbar.n)
    duration = getattr(pbar, "end_time", time.time()) - getattr(pbar, "start_time", 0.0)

    # --- Generate or capture install script ---
    install_sh = stagedir / ".lpm-install.sh"
    install_embedded = False
    install_spec = scal.get("INSTALL", "").strip()

    if install_spec:
        candidates: List[Path] = []
        spec_path = Path(install_spec)
        if spec_path.is_absolute():
            candidates.append(spec_path)
        else:
            candidates.append(srcroot / spec_path)
            if script_dir != srcroot:
                candidates.append(script_dir / spec_path)

        for candidate in candidates:
            if not candidate.exists() or not candidate.is_file():
                continue
            try:
                body = candidate.read_text(encoding="utf-8")
            except Exception as exc:
                warn(f"Failed to read install script {candidate}: {exc}")
                continue

            log(f"[lpm] Embedding install hooks from {candidate.name}")
            with install_sh.open("w", encoding="utf-8") as f:
                f.write("#!/bin/bash\n")
                f.write("set -euo pipefail\n\n")
                f.write("action=${1:-install}\n")
                f.write("new_full=${2:-}\n")
                f.write("old_full=${3:-}\n")
                f.write("source /dev/stdin <<'__LPM_INSTALL_BODY__'\n")
                if body:
                    f.write(body)
                    if not body.endswith("\n"):
                        f.write("\n")
                f.write("__LPM_INSTALL_BODY__\n\n")
                f.write("if declare -f post_install >/dev/null; then\n")
                f.write("  post_install \"$new_full\"\n")
                f.write("fi\n")
                f.write("if [[ \"$action\" == \"upgrade\" ]] && declare -f post_upgrade >/dev/null; then\n")
                f.write("  post_upgrade \"$new_full\" \"$old_full\"\n")
                f.write("fi\n")
            install_sh.chmod(0o755)
            install_embedded = True
            break

        if not install_embedded:
            warn(f"install script '{install_spec}' requested but not found; falling back to default installer")

    if not install_embedded:
        try:
            custom = subprocess.run(
                ["bash", "-c", f'source "{script_path}"; declare -f install_script'],
                capture_output=True,
                text=True,
            )
            if custom.stdout.strip():
                log(f"[lpm] Embedding custom install_script() from {script.name}")
                with install_sh.open("w", encoding="utf-8") as f:
                    f.write("#!/bin/sh\nset -e\n")
                    f.write(custom.stdout)
                    f.write("\ninstall_script \"$@\"\n")
                install_sh.chmod(0o755)
            else:
                script_text = _resolve_lpm_attr(
                    "generate_install_script", generate_install_script
                )(stagedir)
                with install_sh.open("w", encoding="utf-8") as f:
                    if script_text.lstrip().startswith("#!"):
                        f.write(script_text)
                        if not script_text.endswith("\n"):
                            f.write("\n")
                    else:
                        f.write("#!/bin/sh\n")
                        f.write("set -e\n")
                        if script_text:
                            f.write(script_text)
                            if not script_text.endswith("\n"):
                                f.write("\n")
                install_sh.chmod(0o755)
        except Exception as e:
            warn(f"Could not embed install script for {name}: {e}")

    # --- Package metadata ---
    meta = PkgMeta(
        name=name, version=version, release=release, arch=arch,
        summary=summary, url=url, license=license_, developer=developer,
        requires=arr.get("REQUIRES", []),
        build_requires=arr.get("BUILD_REQUIRES", []),
        provides=provides_list,
        provides_by_package=provides_by_package,
        conflicts=arr.get("CONFLICTS", []),
        obsoletes=arr.get("OBSOLETES", []),
        recommends=arr.get("RECOMMENDS", []),
        suggests=arr.get("SUGGESTS", []),
        kernel=kernel,
        mkinitcpio_preset=mkinitcpio_preset,
    )

    outdir = script_dir if outdir is None else outdir
    out = outdir / f"{meta.name}-{meta.version}-{meta.release}.{meta.arch}{EXT}"
    _resolve_lpm_attr("build_package", build_package)(stagedir, meta, out, sign=True)
    split_records: List[Tuple[Path, PkgMeta]] = []
    try:
        if split_record_path.exists():
            for line in split_record_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    path = Path(data.get("path", "")).resolve()
                    meta_dict = data.get("meta", {})
                    meta_dict.setdefault("name", meta.name)
                    meta_dict.setdefault("version", meta.version)
                    meta_dict.setdefault("release", meta.release)
                    meta_dict.setdefault("arch", meta.arch)
                    meta_dict.setdefault("developer", meta.developer)
                    pkg_meta = PkgMeta.from_dict(meta_dict)
                    split_records.append((path, pkg_meta))
                except Exception as e:
                    warn(f"Failed to parse split package record: {e}")
    finally:
        for tmp in tmp_files:
            with contextlib.suppress(Exception):
                tmp.unlink()
        with contextlib.suppress(Exception):
            helper_path.unlink()

    if maintainer_mode.is_enabled():
        try:
            maint_result = maintainer_mode.handle_lpmbuild(
                primary=(meta, out),
                split_records=split_records,
                script_path=script_path,
                source_tree=srcroot if srcroot.exists() else None,
            )
        except Exception as exc:
            warn(f"Maintainer mode error: {exc}")
        else:
            maintainer_mode.generate_indexes(maint_result, gen_index)
            maintainer_mode.finalize_git(maint_result)

    if on_built_package:
        try:
            on_built_package(out, meta)
        except Exception as exc:
            warn(f"[deps] on_built_package callback failed for {meta.name}: {exc}")
        for split_path, split_meta in split_records:
            try:
                on_built_package(split_path, split_meta)
            except Exception as exc:
                warn(
                    f"[deps] on_built_package callback failed for {split_meta.name}: {exc}"
                )

    if prompt_install:
        _resolve_lpm_attr("prompt_install_pkg", prompt_install_pkg)(
            out, kind="dependency" if is_dep else "package", default=prompt_default
        )
        for split_path, _split_meta in split_records:
            _resolve_lpm_attr("prompt_install_pkg", prompt_install_pkg)(
                split_path, kind="split package", default=prompt_default
            )
    return out, duration, phase_count, split_records

# =========================== CLI commands =====================================
_PRIVILEGED_COMMANDS = {"install", "installpkg", "remove", "removepkg", "upgrade", "upgradepkg", "rollback", "systemiso"}
_STATE_COMMANDS = {
    "autoremove",
    "bootstrap",
    "bootstrap-chroot",
    "build",
    "buildchroot",
    "buildpkg",
    "systemiso",
    "clean",
    "files",
    "history",
    "info",
    "install",
    "installpkg",
    "list",
    "pins",
    "protected",
    "rebuild",
    "removepkg",
    "remove",
    "repoadd",
    "repodel",
    "repolist",
    "rollback",
    "search",
    "snapshots",
    "splitpkg",
    "verify",
}


def _state_setup_permission_message(exc: PermissionError) -> str:
    state_dir = _resolve_lpm_attr("STATE_DIR", STATE_DIR)
    conf_file = _resolve_lpm_attr("CONF_FILE", CONF_FILE)
    return (
        f"Unable to initialize LPM state under {state_dir}: {exc}. "
        f"Run 'sudo lpm setup' to create {conf_file} and the LPM state tree, "
        "or re-run this command with sufficient privileges."
    )


def _initialize_cli_state() -> None:
    with operation_phase(privileged=True):
        _resolve_lpm_attr("initialize_state", initialize_state)()


def cmd_repolist(_):
    for r in sorted(list_repos(), key=lambda x:x.priority):
        print(f"{r.name:15} {r.url} (prio {r.priority})")

def cmd_repoadd(a): add_repo(a.name,a.url,a.priority)
def cmd_repodel(a): del_repo(a.name)

def cmd_search(a):
    uni=load_universe()
    pats=a.patterns or ["*"]
    rows=[]
    for name,lst in uni.items():
        if any(fnmatch.fnmatch(name,p) for p in pats):
            p=lst[0]
            rows.append((name,p.version,p.summary))
    for n,v,s in sorted(rows): print(f"{n:30} {v:10} {s}")

def cmd_info(a):
    uni=load_universe()
    for name in a.names:
        lst=uni.get(name,[])
        if not lst: print(f"{name}: not found"); continue
        p=lst[0]
        print(f"Name:       {p.name}")
        print(f"Version:    {p.version}-{p.release}.{p.arch}")
        print(f"Summary:    {p.summary}")
        print(f"Homepage:   {p.url}")
        print(f"License:    {p.license}")
        print(f"Provides:   {', '.join(p.provides) or '-'}")
        print(f"Requires:   {', '.join(p.requires) or '-'}")
        print(f"BuildReqs:  {', '.join(p.build_requires) or '-'}")
        print(f"Conflicts:  {', '.join(p.conflicts) or '-'}")
        print(f"Obsoletes:  {', '.join(p.obsoletes) or '-'}")
        print(f"Recommends: {', '.join(p.recommends) or '-'}")
        print(f"Suggests:   {', '.join(p.suggests) or '-'}")
        print(f"Blob:       {p.blob or '-'}")

def cmd_install(a):
    mode = "never" if getattr(a, "no_delta", False) else _config.USE_DELTAS
    with _delta_mode(mode):
        root = Path(a.root or DEFAULT_ROOT)
        u = build_universe()
        goals = a.names
        try:
            plan = solve(goals, u)
        except ResolutionError as e:
            die(f"dependency resolution failed: {e}")
        log("[plan] install order:")
        for p in plan:
            log(f"  - {p.name}-{p.version}")
        if a.dry_run:
            return
        noverify = a.no_verify or os.environ.get("LPM_NO_VERIFY") == "1"
        allow_fallback = ALLOW_LPMBUILD_FALLBACK if a.allow_fallback is None else a.allow_fallback

        snapshot_id = None
        snapshot_archive = None
        try:
            affected: Set[Path] = set()
            for p in plan:
                try:
                    blob, _ = fetch_blob(p)
                    _, mani = read_package_meta(blob)
                    for e in mani:
                        path = e["path"] if isinstance(e, dict) else e
                        affected.add(root / path.lstrip("/"))
                except Exception as e:
                    warn(f"could not prepare snapshot for {p.name}: {e}")
            tag = "install-" + "-".join([p.name for p in plan])
            snapshot_archive = create_snapshot(tag, affected)
            conn = db()
            row = conn.execute("SELECT id FROM snapshots WHERE archive=?", (snapshot_archive,)).fetchone()
            conn.close()
            if row:
                snapshot_id = row[0]
        except Exception as e:
            warn(f"snapshot failed: {e}")

        try:
            do_install(
                plan,
                root,
                a.dry_run,
                verify=(not noverify),
                force=a.force,
                explicit=set(a.names),
                allow_fallback=allow_fallback,
            )
        except SystemExit:
            if snapshot_id is not None:
                warn(f"Snapshot {snapshot_id} created at {snapshot_archive} for rollback.")
            raise


def cmd_remove(a):
    root = Path(a.root or DEFAULT_ROOT)
    snapshot_id = None
    snapshot_archive = None
    if not a.dry_run:
        conn = db()
        affected: Set[Path] = set()
        for n in a.names:
            row = conn.execute("SELECT manifest FROM installed WHERE name=?", (n,)).fetchone()
            if row:
                mani = json.loads(row[0])
                for e in mani:
                    path = e["path"] if isinstance(e, dict) else e
                    affected.add(root / path.lstrip("/"))
        conn.close()
        tag = "remove-" + "-".join(a.names)
        snapshot_archive = create_snapshot(tag, affected)
        conn = db()
        row = conn.execute("SELECT id FROM snapshots WHERE archive=?", (snapshot_archive,)).fetchone()
        conn.close()
        if row:
            snapshot_id = row[0]
    try:
        do_remove(a.names, root, a.dry_run, force=a.force)
    except SystemExit:
        if snapshot_id is not None:
            warn(f"Snapshot {snapshot_id} created at {snapshot_archive} for rollback.")
        raise


def cmd_autoremove(a):
    root = Path(a.root or DEFAULT_ROOT)
    autoremove(root, a.dry_run)

def cmd_upgrade(a):
    mode = "never" if getattr(a, "no_delta", False) else _config.USE_DELTAS
    with _delta_mode(mode):
        root = Path(a.root or DEFAULT_ROOT)
        noverify = a.no_verify or os.environ.get("LPM_NO_VERIFY") == "1"
        dry = a.dry_run
        force = a.force
        allow_fallback = ALLOW_LPMBUILD_FALLBACK if a.allow_fallback is None else a.allow_fallback

        u = build_universe()
        goals: List[str] = []
        if not a.names:
            for n, meta in u.installed.items():
                goals.append(f"{n} ~= {meta['version']}")
        else:
            goals += a.names

        try:
            plan = solve(goals, u)
        except ResolutionError:
            if not allow_fallback:
                die(
                    "SAT solver could not find an upgrade set and GitLab fallback is disabled. "
                    "Re-run with --allow-fallback or enable ALLOW_LPMBUILD_FALLBACK in lpm.conf"
                )
            warn("SAT solver failed to find upgrade set, falling back to GitLab fetch...")
            for dep in a.names:
                built = build_from_gitlab(dep)
                installpkg(
                    built,
                    root=root,
                    dry_run=dry,
                    verify=(not noverify),
                    force=force,
                    explicit=True,
                    allow_fallback=allow_fallback,
                )
            return

        upgrades: List[PkgMeta] = []
        for p in plan:
            cur = u.installed.get(p.name)
            if not cur or cmp_semver(p.version, cur["version"]) > 0:
                upgrades.append(p)

        if not upgrades:
            ok("Nothing to do.")
            return

        snapshot_id = None
        snapshot_archive = None
        if not dry:
            affected: Set[Path] = set()
            conn = db()
            for p in upgrades:
                row = conn.execute("SELECT manifest FROM installed WHERE name=?", (p.name,)).fetchone()
                if row:
                    mani = json.loads(row[0])
                    for e in mani:
                        path = e["path"] if isinstance(e, dict) else e
                        affected.add(root / path.lstrip("/"))
            conn.close()
            for p in upgrades:
                try:
                    blob, _ = fetch_blob(p)
                    _, mani = read_package_meta(blob)
                    for e in mani:
                        path = e["path"] if isinstance(e, dict) else e
                        affected.add(root / path.lstrip("/"))
                except Exception as e:
                    warn(f"could not prepare snapshot for {p.name}: {e}")
            tag = "upgrade-" + "-".join([p.name for p in upgrades])
            snapshot_archive = create_snapshot(tag, affected)
            conn = db()
            row = conn.execute("SELECT id FROM snapshots WHERE archive=?", (snapshot_archive,)).fetchone()
            conn.close()
            if row:
                snapshot_id = row[0]

            def svc_worker(p: PkgMeta):
                _cleanup_upgrade_service_files(p, root, u.installed, dry_run=dry)

            max_workers = min(8, len(upgrades))
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                list(ex.map(svc_worker, upgrades))

        explicit_names = {n for n, m in u.installed.items() if m.get("explicit")}
        explicit_names |= set(a.names)
        try:
            do_install(
                upgrades,
                root,
                dry,
                verify=(not noverify),
                force=force,
                explicit=explicit_names,
                allow_fallback=allow_fallback,
            )
        except SystemExit:
            if snapshot_id is not None:
                warn(f"Snapshot {snapshot_id} created at {snapshot_archive} for rollback.")
            raise

def cmd_files(a):
    conn = db()
    row = conn.execute("SELECT manifest FROM installed WHERE name=?", (a.name,)).fetchone()
    conn.close()
    if not row:
        warn(f"{a.name} not installed")
        return
    mani = json.loads(row[0]) if row[0] else []
    for e in mani:
        path = e["path"] if isinstance(e, dict) else e
        print(path)

def _format_install_time(ts: Optional[int]) -> str:
    if not ts:
        return "unknown"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except (OverflowError, ValueError, OSError):
        return "unknown"


def cmd_list_installed(_):
    conn = db()
    rows = list(
        conn.execute(
            "SELECT name,version,release,arch,install_time,explicit FROM installed ORDER BY name"
        )
    )
    conn.close()

    if not rows:
        print("No packages installed.")
        return

    table_rows = []
    explicit_count = 0
    for name, version, release, arch, installed_ts, explicit in rows:
        if explicit:
            explicit_count += 1
        table_rows.append(
            (
                name,
                f"{version}-{release}",
                arch,
                _format_install_time(installed_ts),
                "explicit" if explicit else "dependency",
            )
        )

    headers = ("Name", "Version", "Arch", "Installed", "Origin")
    widths = [
        max(len(header), *(len(row[idx]) for row in table_rows)) for idx, header in enumerate(headers)
    ]

    def fmt_row(row):
        return "  ".join(col.ljust(width) for col, width in zip(row, widths))

    print(f"Installed packages: {len(rows)} total")
    print(fmt_row(headers))
    print("  ".join("-" * w for w in widths))
    for row in table_rows:
        print(fmt_row(row))

    deps = len(rows) - explicit_count
    print()
    print(f"Explicit: {explicit_count}    Dependencies: {deps}")

def cmd_snapshots(a):
    if a.delete:
        conn = db()
        for sid in a.delete:
            row = conn.execute("SELECT archive FROM snapshots WHERE id=?", (sid,)).fetchone()
            if row:
                try:
                    Path(row[0]).unlink(missing_ok=True)
                except Exception as e:
                    warn(f"rm {row[0]}: {e}")
                conn.execute("DELETE FROM snapshots WHERE id=?", (sid,))
        conn.commit()
        conn.close()

    if a.prune:
        prune_snapshots(MAX_SNAPSHOTS)

    conn = db()
    rows = list(conn.execute("SELECT id,ts,tag,archive FROM snapshots ORDER BY id DESC"))
    conn.close()
    if not rows:
        print("No snapshots found")
    else:
        for sid, ts, tag, archive in rows:
            t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            print(f"{sid:4} {t} {tag} {archive}")

def cmd_rollback(a):
    conn = db()
    if a.snapshot_id is not None:
        row = conn.execute("SELECT id,tag,archive FROM snapshots WHERE id=?", (a.snapshot_id,)).fetchone()
        if not row:
            die(f"snapshot {a.snapshot_id} not found")
    else:
        row = conn.execute("SELECT id,tag,archive FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            die("no snapshots available")
    sid, tag, archive = row
    restore_snapshot(Path(archive))
    conn.execute(
        "INSERT INTO history(ts,action,name,from_ver,to_ver,details) VALUES(?,?,?,?,?,?)",
        (int(time.time()), "rollback", tag, None, None, archive),
    )
    conn.commit()
    ok(f"Rolled back to snapshot {sid} ({tag})")

def cmd_history(_):
    conn=db()
    for ts,act,name,frm,to in conn.execute("SELECT ts,action,name,from_ver,to_ver FROM history ORDER BY id DESC LIMIT 200"):
        t=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        if act=="install":
            print(f"{t}  install  {name} -> {to}")
        elif act=="remove":
            print(f"{t}  remove   {name} ({frm})")
        elif act=="rollback":
            print(f"{t}  rollback {name}")
        else:
            print(f"{t}  {act}  {name}")

def cmd_verify(a):
    root = Path(a.root or DEFAULT_ROOT)
    conn = db()
    pkgs = [(n, json.loads(mani)) for n, mani in conn.execute("SELECT name,manifest FROM installed")]
    bad = 0

    def _verify_pkg(pkg):
        n, mani = pkg
        local_bad = 0
        for entry in mani:
            path = entry["path"] if isinstance(entry, dict) else entry
            f = root / path.lstrip("/")
            if not f.exists():
                print(f"[MISSING] {n}: {path}")
                local_bad += 1
                continue
            if isinstance(entry, dict):
                actual_size = f.stat().st_size
                if actual_size != entry["size"]:
                    print(f"[SIZE MISMATCH] {n}: {path} expected {entry['size']}, got {actual_size}")
                    local_bad += 1
                actual_hash = sha256sum(f)
                if actual_hash != entry["sha256"]:
                    print(f"[HASH MISMATCH] {n}: {path}")
                    local_bad += 1
        return local_bad

    with ThreadPoolExecutor(max_workers=min(8, len(pkgs) or 1)) as ex:
        futures = [ex.submit(_verify_pkg, pkg) for pkg in pkgs]
        for fut in progress_bar(
            as_completed(futures),
            total=len(futures),
            desc="Verifying",
            unit="pkg",
        ):
            bad += fut.result()

    if bad == 0:
        ok("All files validated successfully")
    else:
        warn(f"{bad} validation errors")


def cmd_pins(a):
    pins=read_json(PIN_FILE)
    if a.action=="list":
        print(json.dumps(pins, indent=2))
    elif a.action=="hold":
        pins.setdefault("hold",[])
        for n in a.names:
            if n not in pins["hold"]: pins["hold"].append(n)
        with operation_phase(privileged=True):
            write_json(PIN_FILE, pins)
        ok("Updated holds")
    elif a.action=="unhold":
        pins.setdefault("hold",[])
        pins["hold"]=[n for n in pins["hold"] if n not in a.names]
        with operation_phase(privileged=True):
            write_json(PIN_FILE, pins)
        ok("Updated holds")
    elif a.action=="prefer":
        pins.setdefault("prefer",{})
        for s in a.prefs:
            if ":" not in s: die("use name:constraint, e.g. openssl:~=3.3")
            name,cons = s.split(":",1)
            pins["prefer"][name]=cons
        with operation_phase(privileged=True):
            write_json(PIN_FILE, pins)
        ok("Updated preferences")

def cmd_build(a):
    stagedir=Path(a.stagedir)
    meta = PkgMeta(
        name=a.name, version=a.version, release=a.release, arch=a.arch,
        summary=a.summary, url=a.url, license=a.license, developer=a.developer,
        requires=a.requires, provides=a.provides, conflicts=a.conflicts,
        obsoletes=a.obsoletes, recommends=a.recommends, suggests=a.suggests
    )
    out = Path(a.output or f"{meta.name}-{meta.version}-{meta.release}.{meta.arch}{EXT}")
    _resolve_lpm_attr("build_package", build_package)(stagedir, meta, out, sign=(not a.no_sign))
    _resolve_lpm_attr("prompt_install_pkg", prompt_install_pkg)(out, default=a.install_default)

def cmd_splitpkg(a):
    stagedir = Path(a.stagedir)

    base_meta_path = os.environ.get("LPM_SPLIT_BASE_META")
    base_meta_file: Optional[Path] = None
    base_meta: Dict[str, object] = {}
    if base_meta_path:
        try:
            base_meta_file = Path(base_meta_path)
            base_meta = read_json(base_meta_file)
        except Exception as e:
            warn(f"Could not read split package defaults: {e}")
            base_meta_file = None

    def _get_default(key: str, fallback=None):
        value = getattr(a, key, None)
        if value is not None:
            return value
        return base_meta.get(key, fallback)

    name = _get_default("name")
    if not name:
        die("splitpkg requires --name or LPM_SPLIT_BASE_META")
    version = _get_default("version", "")
    if not version:
        die("splitpkg missing version (set --version or VERSION in defaults)")
    release = _get_default("release", "1")
    arch = _get_default("arch", ARCH or "noarch") or "noarch"
    summary = _get_default("summary", "")
    url = _get_default("url", "")
    license_ = _get_default("license", "")
    developer = _get_default("developer", "")

    def _merge_list(opt_name: str) -> List[str]:
        opt = getattr(a, opt_name, None)
        if opt:
            return [str(x) for x in opt]
        base = base_meta.get(opt_name)
        if isinstance(base, list):
            return [str(x) for x in base]
        return []

    requires = _merge_list("requires")
    build_requires = _merge_list("build_requires")
    provides = _merge_list("provides")
    conflicts = _merge_list("conflicts")
    obsoletes = _merge_list("obsoletes")
    recommends = _merge_list("recommends")
    suggests = _merge_list("suggests")
    kernel = bool(_get_default("kernel", False))
    mkinitcpio_preset = _get_default("mkinitcpio_preset")

    meta = PkgMeta(
        name=name,
        version=str(version),
        release=str(release),
        arch=str(arch),
        summary=str(summary),
        url=str(url),
        license=str(license_),
        developer=str(developer),
        requires=requires,
        build_requires=build_requires,
        provides=provides,
        conflicts=conflicts,
        obsoletes=obsoletes,
        recommends=recommends,
        suggests=suggests,
        kernel=kernel,
        mkinitcpio_preset=mkinitcpio_preset if mkinitcpio_preset else None,
    )

    outdir = Path(a.outdir or os.environ.get("LPM_SPLIT_OUTDIR") or stagedir.parent)
    out: Path
    if a.output:
        out = Path(a.output)
    else:
        out = outdir / f"{meta.name}-{meta.version}-{meta.release}.{meta.arch}{EXT}"
    prepare_directory(out.parent, privileged=False)

    install_sh = stagedir / ".lpm-install.sh"
    if install_sh.exists():
        with contextlib.suppress(Exception):
            install_sh.chmod(0o755)
    else:
        install_spec: Optional[str] = None
        for key in ("INSTALL", "install"):
            value = base_meta.get(key)
            if isinstance(value, str) and value.strip():
                install_spec = value.strip()
                break

        embedded = False
        if install_spec:
            candidates: List[Path] = []
            spec_path = Path(install_spec)
            if spec_path.is_absolute():
                candidates.append(spec_path)
            else:
                candidates.append(stagedir / spec_path)
                if base_meta_file is not None:
                    candidates.append(base_meta_file.parent / spec_path)

            for candidate in candidates:
                if not candidate.exists() or not candidate.is_file():
                    continue
                try:
                    body = candidate.read_text(encoding="utf-8")
                except Exception as exc:
                    warn(f"Failed to read install script {candidate}: {exc}")
                    continue

                try:
                    with install_sh.open("w", encoding="utf-8") as f:
                        f.write("#!/bin/bash\n")
                        f.write("set -euo pipefail\n\n")
                        f.write("action=${1:-install}\n")
                        f.write("new_full=${2:-}\n")
                        f.write("old_full=${3:-}\n")
                        f.write("source /dev/stdin <<'__LPM_INSTALL_BODY__'\n")
                        if body:
                            f.write(body)
                            if not body.endswith("\n"):
                                f.write("\n")
                        f.write("__LPM_INSTALL_BODY__\n\n")
                        f.write("if declare -f post_install >/dev/null; then\n")
                        f.write("  post_install \"$new_full\"\n")
                        f.write("fi\n")
                        f.write("if [[ \"$action\" == \"upgrade\" ]] && declare -f post_upgrade >/dev/null; then\n")
                        f.write("  post_upgrade \"$new_full\" \"$old_full\"\n")
                        f.write("fi\n")
                    install_sh.chmod(0o755)
                    embedded = True
                except Exception as exc:
                    warn(f"Could not embed install script from {candidate}: {exc}")
                    continue

                if embedded:
                    break

            if not embedded:
                warn(
                    f"install script '{install_spec}' requested but not found; "
                    "falling back to default installer"
                )

        if not install_sh.exists():
            try:
                script_text = _resolve_lpm_attr(
                    "generate_install_script", generate_install_script
                )(stagedir)
                with install_sh.open("w", encoding="utf-8") as f:
                    if script_text.lstrip().startswith("#!"):
                        f.write(script_text)
                        if not script_text.endswith("\n"):
                            f.write("\n")
                    else:
                        f.write("#!/bin/sh\n")
                        f.write("set -e\n")
                        if script_text:
                            f.write(script_text)
                            if not script_text.endswith("\n"):
                                f.write("\n")
                install_sh.chmod(0o755)
            except Exception as exc:
                warn(f"Could not embed install script for {name}: {exc}")

    _resolve_lpm_attr("build_package", build_package)(stagedir, meta, out, sign=(not a.no_sign))

    record_path = os.environ.get("LPM_SPLIT_RECORD")
    if record_path:
        try:
            rec = {"path": str(out), "meta": dataclasses.asdict(meta)}
            with open(record_path, "a", encoding="utf-8") as f:
                json.dump(rec, f)
                f.write("\n")
        except Exception as e:
            warn(f"Could not record split package metadata: {e}")

    ok(f"Built split package {out}")

def cmd_buildpkg(a):
    def _get_buildpkg_worker_count() -> int:
        value = CONF.get("BUILDPKG_WORKERS")
        if value is not None:
            try:
                workers = int(value)
            except (TypeError, ValueError):
                workers = 0
            else:
                if workers > 0:
                    return workers
        cpu_workers = os.cpu_count() or 1
        return max(2, min(8, cpu_workers))

    worker_count = _get_buildpkg_worker_count()
    cpu_override = _parse_cpu_overrides(getattr(a, "overrides", []))

    if a.python_pip:
        if a.script:
            die("Cannot specify both a .lpmbuild script and --python-pip")
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future = executor.submit(
                _resolve_lpm_attr("build_python_package_from_pip", build_python_package_from_pip),
                a.python_pip,
                a.outdir,
                include_deps=not a.no_deps,
                cpu_overrides=cpu_override,
            )
            out, meta, duration = future.result()
        _resolve_lpm_attr("prompt_install_pkg", prompt_install_pkg)(out, default=a.install_default)
        print_build_summary(meta, out, duration, len(meta.requires), 1)
        ok(f"Built {out}")
        return

    if not a.script:
        die("buildpkg requires a .lpmbuild script or --python-pip")

    raw_script = Path(a.script)
    script_path = raw_script
    if not script_path.exists():
        candidate_name = raw_script.name
        if candidate_name.endswith(".lpmbuild"):
            candidate_name = candidate_name[:-len(".lpmbuild")]

        local_candidate = _resolve_local_lpmbuild_script(candidate_name)
        if local_candidate.exists():
            script_path = local_candidate
            ok(f"Resolved local lpmbuild script: {script_path}")
        else:
            fetched_script = Path(f"/tmp/lpm-{candidate_name}.lpmbuild")
            fetch_fn = _resolve_lpm_attr("fetch_lpmbuild", fetch_lpmbuild)
            try:
                script_path = fetch_fn(candidate_name, fetched_script)
                ok(f"Resolved repository lpmbuild script: {script_path}")
            except Exception:
                die(f".lpmbuild script not found: {raw_script}")

    run_lpmbuild_fn = _resolve_lpm_attr("run_lpmbuild", run_lpmbuild)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future = executor.submit(
            run_lpmbuild_fn,
            script_path,
            a.outdir,
            build_deps=not a.no_deps,
            force_rebuild=a.force_rebuild,
            prompt_default=a.install_default,
            executor=executor if worker_count > 1 else None,
            cpu_overrides=cpu_override,
        )
        out, duration, phases, splits = future.result()

    if out and out.exists():
        meta, _ = _resolve_lpm_attr("read_package_meta", read_package_meta)(out)
        print_build_summary(meta, out, duration, len(meta.requires), phases)
        if splits:
            for spath, smeta in splits:
                ok(f"Split: {spath} ({smeta.name})")
        ok(f"Built {out}")
    else:
        die(f"Build failed for {a.script}")


def _reverse_dependents_from_installed(conn: sqlite3.Connection) -> Dict[str, Set[str]]:
    rows = list(conn.execute("SELECT name,requires FROM installed ORDER BY name"))
    installed: Dict[str, Dict[str, object]] = {}
    for name, requires in rows:
        req_list: List[str] = []
        if requires:
            try:
                loaded = json.loads(requires)
                if isinstance(loaded, list):
                    req_list = [str(item) for item in loaded if item]
            except Exception:
                req_list = []
        installed[name] = {"requires": req_list, "provides": []}

    providers = _installed_provider_map(installed)
    reverse: Dict[str, Set[str]] = {}
    for pkg_name, meta in installed.items():
        for req in meta.get("requires", []) or []:
            try:
                expr = parse_dep_expr(req)
            except Exception:
                continue
            matched = _match_dep_expr_against_installed(expr, installed, providers)
            for dep_name in matched:
                reverse.setdefault(dep_name, set()).add(pkg_name)
    return reverse


def _resolve_local_lpmbuild_script(pkg_name: str) -> Path:
    return Path("packages") / pkg_name / f"{pkg_name}.lpmbuild"


def _order_rebuild_targets(
    root_name: str,
    reverse: Dict[str, Set[str]],
    cycle_policy: str = "fail",
) -> Tuple[List[str], List[List[str]]]:
    levels: Dict[str, int] = {root_name: 0}
    queue: deque[str] = deque([root_name])
    while queue:
        cur = queue.popleft()
        for dep in sorted(reverse.get(cur, set())):
            if dep in levels:
                continue
            levels[dep] = levels[cur] + 1
            queue.append(dep)

    closure = sorted(levels.keys())
    closure_set = set(closure)
    subset_edges: Dict[str, Set[str]] = {
        pkg: (reverse.get(pkg, set()) & closure_set) for pkg in closure
    }

    index_map = {pkg: idx for idx, pkg in enumerate(closure)}
    indices: Dict[str, int] = {}
    lowlink: Dict[str, int] = {}
    stack: List[str] = []
    on_stack: Set[str] = set()
    next_index = 0
    sccs: List[List[str]] = []

    def strongconnect(node: str) -> None:
        nonlocal next_index
        indices[node] = next_index
        lowlink[node] = next_index
        next_index += 1
        stack.append(node)
        on_stack.add(node)
        for child in sorted(subset_edges[node]):
            if child not in indices:
                strongconnect(child)
                lowlink[node] = min(lowlink[node], lowlink[child])
            elif child in on_stack:
                lowlink[node] = min(lowlink[node], indices[child])
        if lowlink[node] == indices[node]:
            component: List[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            sccs.append(sorted(component))

    for pkg in closure:
        if pkg not in indices:
            strongconnect(pkg)

    cycle_groups = [
        group
        for group in sccs
        if len(group) > 1 or (len(group) == 1 and group[0] in subset_edges[group[0]])
    ]
    cycle_groups.sort(key=lambda g: g[0])
    if cycle_groups and cycle_policy == "fail":
        return [], cycle_groups

    component_of: Dict[str, int] = {}
    components = sorted(sccs, key=lambda g: g[0])
    for comp_id, group in enumerate(components):
        for member in group:
            component_of[member] = comp_id

    comp_indegree: Dict[int, int] = {i: 0 for i in range(len(components))}
    comp_edges: Dict[int, Set[int]] = {i: set() for i in range(len(components))}
    for src in closure:
        src_comp = component_of[src]
        for dst in sorted(subset_edges[src]):
            dst_comp = component_of[dst]
            if src_comp == dst_comp or dst_comp in comp_edges[src_comp]:
                continue
            comp_edges[src_comp].add(dst_comp)
            comp_indegree[dst_comp] += 1

    zero = sorted(
        [cid for cid, deg in comp_indegree.items() if deg == 0],
        key=lambda cid: (min(levels[n] for n in components[cid]), components[cid][0], index_map[components[cid][0]]),
    )
    ordered: List[str] = []
    while zero:
        cid = zero.pop(0)
        ordered.extend(components[cid])
        for dst_cid in sorted(comp_edges[cid], key=lambda c: (components[c][0], c)):
            comp_indegree[dst_cid] -= 1
            if comp_indegree[dst_cid] == 0:
                zero.append(dst_cid)
                zero.sort(
                    key=lambda c: (min(levels[n] for n in components[c]), components[c][0], index_map[components[c][0]])
                )
    return ordered, cycle_groups


def cmd_rebuild(a):
    conn = db()
    rows = list(
        conn.execute(
            "SELECT name,version,conflicts,obsoletes,provides FROM installed ORDER BY name"
        )
    )
    installed_meta: Dict[str, dict] = {}
    for name, version, conflicts_raw, obsoletes_raw, provides_raw in rows:
        def _decode_list(raw: Any) -> List[str]:
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                except Exception:
                    return [raw] if raw else []
                if isinstance(parsed, list):
                    return [str(item) for item in parsed if item]
            return []

        installed_meta[name] = {
            "version": version or "",
            "conflicts": _decode_list(conflicts_raw),
            "obsoletes": _decode_list(obsoletes_raw),
            "provides": _decode_list(provides_raw),
        }
    installed_names = set(installed_meta.keys())
    reverse = _reverse_dependents_from_installed(conn)
    conn.close()

    if a.name not in installed_names:
        die(f"Package is not installed: {a.name}")

    topo, cycle_groups = _order_rebuild_targets(a.name, reverse, cycle_policy=a.cycle_policy)
    if cycle_groups and a.cycle_policy == "fail":
        groups = "; ".join(", ".join(group) for group in cycle_groups)
        die(
            "Cycle detected in rebuild dependency graph. "
            f"Use --cycle-policy group to continue. Cycle groups: {groups}"
        )
    if cycle_groups and a.cycle_policy == "group":
        for group in cycle_groups:
            print(f"[rebuild cycle-group] {', '.join(group)}")

    providers = _installed_provider_map(installed_meta)
    conflict_pairs: Set[Tuple[str, str]] = set()
    for pkg in topo:
        meta = installed_meta.get(pkg, {})
        for raw in (meta.get("conflicts", []) or []) + (meta.get("obsoletes", []) or []):
            if not raw:
                continue
            try:
                expr = parse_dep_expr(raw)
            except Exception:
                warn(f"Invalid rebuild conflict expression in {pkg}: {raw}")
                continue
            for matched_pkg in _match_dep_expr_against_installed(expr, installed_meta, providers):
                if matched_pkg not in topo or matched_pkg == pkg:
                    continue
                conflict_pairs.add(tuple(sorted((pkg, matched_pkg))))
    if conflict_pairs and a.conflict_policy == "fail":
        formatted = ", ".join(f"{left} <-> {right}" for left, right in sorted(conflict_pairs))
        die(f"Rebuild preflight failed: conflicting targets detected: {formatted}")
    if conflict_pairs and a.conflict_policy == "skip":
        for left, right in sorted(conflict_pairs):
            warn(f"Skipping conflicting pair due to --conflict-policy=skip: {left} <-> {right}")

    missing: List[Tuple[str, Path]] = []
    scripts: Dict[str, Path] = {}
    for pkg in topo:
        script = _resolve_local_lpmbuild_script(pkg)
        if not script.exists():
            missing.append((pkg, script))
            continue
        scripts[pkg] = script
    if missing:
        formatted = ", ".join([f"{pkg} ({path})" for pkg, path in missing])
        die(f"Missing .lpmbuild scripts for rebuild targets: {formatted}")

    total = len(topo)
    for idx, pkg in enumerate(topo, start=1):
        print(f"[rebuild {idx}/{total}] {pkg}")
        run_lpmbuild(
            scripts[pkg],
            a.outdir,
            build_deps=not a.no_deps,
            force_rebuild=a.force_rebuild,
            prompt_default=a.install_default,
        )
    ok(f"Rebuilt packages ({total}): {', '.join(topo)}")


def cmd_genindex(a):
    repo_dir = Path(a.repo_dir)
    gen_index(repo_dir, a.base_url, arch_filter=a.arch)

def cmd_createiso(a):
    from .live_iso import build_live_iso

    result = build_live_iso(
        a.source_root or "/", a.output, volume_id=a.volume_id,
        architecture=a.architecture, kernel=a.kernel, initramfs=a.initramfs,
        dry_run=a.dry_run, staging_root=a.staging_root,
    )
    if a.dry_run:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        ok(f"Created bootable ISO image at {result['output']}")


def cmd_systemiso(a):
    from .system_iso import build_system_iso

    result = build_system_iso(a)
    print(json.dumps(result, indent=2, sort_keys=True))

def cmd_clean_cache(_):
    cache_dir = _current_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    if cache_dir.exists():
        for p in cache_dir.iterdir():
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
        ok("Removed cached blobs")
    else:
        log("No cache directory")

def cmd_fileremove(a):
    root = Path(a.root or DEFAULT_ROOT)

    def worker(name: str):
        removepkg(name=name, root=root, dry_run=a.dry_run, force=a.force)

    max_workers = min(8, len(a.names))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_map = {ex.submit(worker, n): n for n in a.names}
        for fut in progress_bar(
            as_completed(future_map),
            total=len(future_map),
            desc="Removing",
            unit="pkg",
            colour="purple",
        ):
            fut.result()

def cmd_fileinstall(a):
    root = Path(a.root or DEFAULT_ROOT)

    files: List[Path] = []
    for fn in a.files:
        file = Path(fn).resolve()
        if not file.exists():
            die(f"Package file not found: {file}")
        files.append(file)

    def worker(f: Path):
        installpkg(
            file=f,
            root=root,
            dry_run=a.dry_run,
            verify=a.verify,
            force=a.force,
            explicit=True,
            hook_failure_mode=getattr(a, "hook_failure_mode", HookFailureMode.STRICT),
        )

    max_workers = min(8, len(files))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_map = {ex.submit(worker, f): f for f in files}
        for fut in progress_bar(
            as_completed(future_map),
            total=len(future_map),
            desc="Installing",
            unit="pkg",
        ):
            fut.result()


def _installed_provider_map(installed: Mapping[str, dict]) -> Dict[str, Set[str]]:
    providers: Dict[str, Set[str]] = {}

    def add(token: str, pkg_name: str) -> None:
        if not token:
            return
        providers.setdefault(token, set()).add(pkg_name)

    for name, meta in installed.items():
        add(name, name)
        for prov in meta.get("provides", []) or []:
            prov = (prov or "").strip()
            if not prov:
                continue
            m = re.match(r"^([A-Za-z0-9._\-+]+)\s*(==|=|>=|<=|>|<|~=?)*\s*(.*)?$", prov)
            if not m:
                continue
            nm, op, ver = m.group(1), (m.group(2) or ""), (m.group(3) or "")
            add(nm, name)
            if nm and op and ver:
                norm_op = "==" if op == "=" else op
                add(f"{nm}{norm_op}{ver}", name)

    return providers


def _match_dep_expr_against_installed(
    expr: DepExpr,
    installed: Mapping[str, dict],
    providers: Mapping[str, Set[str]],
) -> Set[str]:
    if expr.kind == "atom":
        atom = expr.atom
        if not atom:
            return set()
        matches: Set[str] = set()
        for pkg_name in providers.get(atom.name, set()):
            meta = installed.get(pkg_name)
            if meta is None:
                continue
            if atom.op and atom.ver:
                if not satisfies(meta.get("version", ""), f"{atom.op}{atom.ver}"):
                    continue
            matches.add(pkg_name)
        return matches

    if expr.kind == "or":
        left = _match_dep_expr_against_installed(expr.left, installed, providers) if expr.left else set()
        right = _match_dep_expr_against_installed(expr.right, installed, providers) if expr.right else set()
        return left | right

    if expr.kind == "and":
        left = _match_dep_expr_against_installed(expr.left, installed, providers) if expr.left else set()
        right = _match_dep_expr_against_installed(expr.right, installed, providers) if expr.right else set()
        if not left or not right:
            return set()
        return left & right

    return set()


def _resolve_obsoletes_against_installed(
    obsoletes: Iterable[str], installed: Mapping[str, dict]
) -> Set[str]:
    providers = _installed_provider_map(installed)
    matches: Set[str] = set()

    for raw in obsoletes:
        if not raw:
            continue
        try:
            expr = parse_dep_expr(raw)
        except Exception:
            warn(f"Invalid obsoletes expression: {raw}")
            continue
        matches.update(_match_dep_expr_against_installed(expr, installed, providers))

    return matches

def installpkg(
    file: Path | Iterable[Path],
    root: Path = Path(DEFAULT_ROOT),
    dry_run: bool = False,
    verify: bool = True,
    force: bool = False,
    explicit: bool = False,
    allow_fallback: bool = ALLOW_LPMBUILD_FALLBACK,
    hook_failure_mode: str = HookFailureMode.STRICT,
    hook_transaction: Optional[HookTransactionManager] = None,
    register_event: bool = True,
) -> PkgMeta | List[PkgMeta]:
    """
    Production-grade .zst package installer with protected package + dep resolution.

    Accepts a single package path or an iterable of paths. When an iterable is
    provided, packages are installed sequentially using a shared hook transaction
    manager and a list of installed :class:`PkgMeta` objects is returned. When a
    single path is provided, the installed :class:`PkgMeta` is returned as
    before.
    """
    global PROTECTED
    PROTECTED = load_protected()

    root = Path(root)
    _require_privileged_default_root(root, "installpkg", "installing to")

    is_single_path = isinstance(file, Path)
    files: List[Path] = [Path(file)] if is_single_path else [Path(f) for f in file]

    with _privileged_default_root_mutation(root, dry_run):
        txn = hook_transaction
        owns_txn = False
        if txn is None and not dry_run:
            txn = HookTransactionManager(
                hooks=load_hooks(LIBLPM_HOOK_DIRS),
                root=root,
                base_env={"LPM_ROOT": str(root)},
                failure_mode=hook_failure_mode,
            )
            owns_txn = True

        def _replace_path(dest: Path) -> None:
            try:
                if dest.is_symlink() or dest.is_file():
                    dest.unlink()
                elif dest.is_dir():
                    shutil.rmtree(dest)
            except FileNotFoundError:
                return

        def _install_single(pkg_file: Path) -> PkgMeta:
            # --- Step 1: Validate extension + magic ---
            if pkg_file.suffix != EXT:
                die(f"{pkg_file.name} is not a {EXT} package")
            try:
                with pkg_file.open("rb") as f:
                    magic = f.read(4)
                if magic != b"\x28\xb5\x2f\xfd":
                    die(f"{pkg_file.name} is not a valid {EXT} (bad magic header)")
            except Exception as e:
                die(f"Cannot read {pkg_file}: {e}")

            # --- Step 2: Signature verification ---
            sig = pkg_file.with_suffix(pkg_file.suffix + ".sig")
            if verify:
                if not sig.exists():
                    die(f"Missing signature: {sig}")
                verify_signature(pkg_file, sig)

                # --- Step 3: Read metadata ---
            meta, mani = read_package_meta(pkg_file)
            if not meta:
                die(f"Invalid package: {pkg_file.name} (no metadata)")
            ok(f"Valid package: {meta.name}-{meta.version}-{meta.release}.{meta.arch}")

            if not arch_compatible(meta.arch, ARCH):
                die(f"Incompatible architecture: {meta.arch} (host: {ARCH})")

            # --- Step 3b: Protected package guard ---
            if meta.name in PROTECTED and not force:
                warn(f"{meta.name} is protected (from {PROTECTED_FILE}) and cannot be installed/upgraded without --force")
                return meta

            # --- Step 3c: Meta-package handler ---
            # If package has REQUIRES but no manifest payload → treat as meta-package
            if not mani or all(e["path"].startswith("/.lpm") for e in mani):
                if meta.requires:
                    log(f"[meta] {meta.name} is a meta-package, resolving deps: {', '.join(meta.requires)}")
                    u = build_universe()
                    try:
                        plan = solve(meta.requires, u)
                    except ResolutionError as e:
                        raise ResolutionError(f"{meta.name}: {e}")
                    do_install(plan, root, dry_run, verify, force, explicit=set(), allow_fallback=allow_fallback)
                    ok(f"Installed meta-package {meta.name}-{meta.version}-{meta.release}.{meta.arch}")
                    return meta


            manifest_paths = _normalize_manifest_paths(mani)

            # --- Step 4: Dry-run ---
            if dry_run:
                log(f"[dry-run] Would install {meta.name}-{meta.version}-{meta.release}.{meta.arch}")
                for e in mani:
                    print(f" -> {e['path']} ({e['size']} bytes)")
                return meta

            # --- Step 5: Transaction (unchanged below) ---
            conn = db()
            installed_state: Dict[str, dict] = {}
            if meta.obsoletes:
                installed_state = db_installed(conn)
                matched_obsoletes = _resolve_obsoletes_against_installed(
                    meta.obsoletes, installed_state
                )
                pending_obsoletes = [
                    obsolete
                    for obsolete in sorted(matched_obsoletes)
                    if obsolete != meta.name
                ]
                if pending_obsoletes:
                    warn(
                        "[lpm] Obsoletes detected during install, but automatic removal is disabled. "
                        f"Remove manually if desired: {', '.join(pending_obsoletes)}"
                    )

            row = conn.execute(
                "SELECT version, release, manifest FROM installed WHERE name=?",
                (meta.name,),
            ).fetchone()
            previous_version = row[0] if row else None
            previous_release = row[1] if row else None
            previous_manifest = json.loads(row[2]) if row and row[2] else []

            if txn is not None:
                if register_event:
                    operation = "Upgrade" if row else "Install"
                    txn.add_package_event(
                        name=meta.name,
                        operation=operation,
                        version=meta.version,
                        release=meta.release,
                        paths=manifest_paths,
                    )

                txn.ensure_pre_transaction()

            with transaction(conn, f"install {meta.name}", dry_run):

                hook_env = {
                    "LPM_PKG": meta.name,
                    "LPM_VERSION": meta.version,
                    "LPM_RELEASE": meta.release,
                    "LPM_ROOT": str(root),
                }
                if previous_version is not None:
                    hook_env["LPM_PREVIOUS_VERSION"] = previous_version
                if previous_release is not None:
                    hook_env["LPM_PREVIOUS_RELEASE"] = previous_release

                run_hook("pre_install", dict(hook_env), failure_mode=hook_failure_mode, package_context=meta.name)

                tmp_root = Path(tempfile.mkdtemp(prefix=f"lpm-{meta.name}-", dir="/tmp"))
                try:
                    manifest = extract_tar(pkg_file, tmp_root)

                    # Validate manifest files
                    for e in mani:
                        f = tmp_root / e["path"].lstrip("/")
                        if not f.exists() and not f.is_symlink():
                            die(f"Manifest missing file: {e['path']}")

                        expected_hash = e.get("sha256")
                        if f.is_symlink() or "link" in e:
                            try:
                                target = os.readlink(f)
                            except OSError:
                                die(f"Manifest missing file: {e['path']}")

                            expected_target = e.get("link")
                            if expected_target is not None and target != expected_target:
                                die(f"Link mismatch for {e['path']}: expected {expected_target}, got {target}")

                            link_hash = hashlib.sha256(target.encode()).hexdigest()
                            payload_hash = None

                            payload_candidate: Optional[Path]
                            if target.startswith("/"):
                                payload_candidate = tmp_root / target.lstrip("/")
                            else:
                                payload_candidate = f.parent / target

                            resolved_payload: Optional[Path] = None
                            if payload_candidate is not None:
                                try:
                                    resolved_payload = payload_candidate.resolve()
                                except (FileNotFoundError, RuntimeError, OSError):
                                    resolved_payload = None

                            if resolved_payload is not None:
                                try:
                                    resolved_payload.relative_to(tmp_root)
                                except ValueError:
                                    resolved_payload = None

                            if (
                                resolved_payload is not None
                                and resolved_payload.exists()
                                and resolved_payload.is_file()
                            ):
                                payload_hash = sha256sum(resolved_payload)

                            actual_hash: Optional[str] = None
                            if payload_hash is not None and (
                                expected_hash is None or expected_hash == payload_hash
                            ):
                                actual_hash = payload_hash
                            elif expected_hash == link_hash:
                                actual_hash = link_hash
                            elif payload_hash is not None:
                                actual_hash = payload_hash
                            else:
                                actual_hash = link_hash
                        else:
                            actual_hash = sha256sum(f)

                        if expected_hash is not None and actual_hash != expected_hash:
                            die(
                                f"Hash mismatch for {e['path']}: expected {expected_hash}, got {actual_hash}"
                            )

                    with operation_phase(privileged=True):
                        # Atomic package replace for upgrades: remove all previously-owned
                        # files before materializing the new payload so stale paths and
                        # conflict prompts do not leak across versions.
                        if previous_version is not None and previous_manifest:
                            for old_entry in previous_manifest:
                                old_path = old_entry.get("path") if isinstance(old_entry, dict) else None
                                if not old_path:
                                    continue
                                dest_old = root / str(old_path).lstrip("/")
                                _replace_path(dest_old)

                        # Atomic installs must replace existing files to avoid partial or half-updated payloads.
                        replace_all = True
                        for e in mani:
                            rel = e["path"].lstrip("/")
                            src = tmp_root / rel
                            dest = root / rel
                            dest.parent.mkdir(parents=True, exist_ok=True)

                            # Path.is_dir() follows symlinks. Handle links
                            # first so directory links such as /bin -> usr/bin
                            # are not materialized as real directories.
                            if src.is_dir() and not src.is_symlink():
                                dest.mkdir(parents=True, exist_ok=True)
                                continue

                            if dest.exists() or dest.is_symlink():
                                _replace_path(dest)

                            if not src.exists() and not src.is_symlink():
                                continue

                            if src.is_symlink():
                                target = os.readlink(src)
                                tmp_link = dest.with_name(f".{dest.name}.link")
                                try:
                                    if tmp_link.exists() or tmp_link.is_symlink():
                                        tmp_link.unlink()
                                except PermissionError:
                                    die(f"Permission denied while handling {dest}")
                                tmp_link.symlink_to(target)
                                tmp_link.rename(dest)
                            else:
                                tmp_dest = dest.with_name(f".{dest.name}.tmp")
                                if tmp_dest.exists():
                                    tmp_dest.unlink()
                                shutil.move(str(src), str(tmp_dest))
                                tmp_dest.rename(dest)

                            if "mode" in e:
                                dest.chmod(e["mode"])

                            if "uid" in e and "gid" in e:
                                try:
                                    os.chown(dest, e["uid"], e["gid"])
                                except PermissionError:
                                    warn(f"Permission denied setting ownership for {dest}")

                        # Handle install scripts
                        install_script_rel = None
                        staged_script = None
                        installed_script = None
                        for candidate_rel in ("/.lpm-install.sh", "/.lpm/install.sh"):
                            candidate_path = tmp_root / candidate_rel.lstrip("/")
                            candidate_installed = root / candidate_rel.lstrip("/")
                            if candidate_path.exists():
                                install_script_rel = candidate_rel
                                staged_script = candidate_path
                                installed_script = candidate_installed
                                break
                            if candidate_installed.exists():
                                install_script_rel = candidate_rel
                                installed_script = candidate_installed
                                break

                        if replace_all and installed_script is not None:
                            _replace_path(installed_script)

                        if install_script_rel is not None and staged_script is not None:
                            try:
                                staged_script.chmod(staged_script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                            except OSError:
                                pass
                            staged_script.rename(installed_script)
                            mani.append(
                                {
                                    "path": install_script_rel,
                                    "mode": installed_script.stat().st_mode,
                                    "uid": installed_script.stat().st_uid,
                                    "gid": installed_script.stat().st_gid,
                                    "sha256": sha256sum(installed_script),
                                }
                            )

                        # Handle delta generation
                        if staged_script is not None and installed_script is not None:
                            try:
                                generate_deltas(pkg_file, meta, mani, staged_script, installed_script)
                            except Exception as e:
                                warn(f"Delta generation failed: {e}")
                                try:
                                    for candidate in (installed_script, staged_script):
                                        candidate.unlink()
                                except FileNotFoundError:
                                    pass
                                if install_script_rel is not None:
                                    mani = [e for e in mani if e["path"] != install_script_rel]

                        if install_script_rel is not None and installed_script is not None and installed_script.exists():
                            install_action = "upgrade" if previous_version is not None else "install"
                            install_env = {**os.environ, **hook_env, "LPM_INSTALL_ACTION": install_action}
                            new_full = f"{meta.version}-{meta.release}"
                            old_full = (
                                f"{previous_version}-{previous_release}"
                                if previous_version is not None and previous_release is not None
                                else ""
                            )
                            try:
                                subprocess.run(
                                    [str(installed_script), install_action, new_full, old_full],
                                    env=install_env,
                                    check=True,
                                )
                            finally:
                                with contextlib.suppress(FileNotFoundError):
                                    installed_script.unlink()
                                mani = [e for e in mani if e["path"] != install_script_rel]

                    # Update DB
                    conn.execute(
                        "REPLACE INTO installed(name,version,release,arch,provides,symbols,requires,manifest,explicit,install_time) VALUES(?,?,?,?,?,?,?,?,?,?)",
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
                        "INSERT INTO history(ts,action,name,from_ver,to_ver,details) VALUES(?,?,?,?,?,?)",
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

                run_hook("post_install", dict(hook_env), failure_mode=hook_failure_mode, package_context=meta.name)

                if previous_version is not None:
                    run_hook("post_upgrade", dict(hook_env), failure_mode=hook_failure_mode, package_context=meta.name)

                # New: init system service integration
                with operation_phase(privileged=True):
                    handle_service_files(meta.name, root, mani)

            ok(f"Installed {meta.name}-{meta.version}-{meta.release}.{meta.arch}")
            return meta

        results = [_install_single(pkg_file) for pkg_file in files]

        if txn is not None and owns_txn:
            txn.run_post_transaction()

        return results[0] if is_single_path else results


def removepkg(
    name: str,
    root: Path = Path(DEFAULT_ROOT),
    dry_run: bool = False,
    force: bool = False,
    hook_transaction: Optional[HookTransactionManager] = None,
    register_event: bool = True,
    hook_failure_mode: str = HookFailureMode.STRICT,
):
    global PROTECTED
    PROTECTED = load_protected()

    root = Path(root)
    _require_privileged_default_root(root, "removepkg", "removing from")
    with _privileged_default_root_mutation(root, dry_run):
        txn = hook_transaction
        owns_txn = False
        if txn is None and not dry_run:
            txn = HookTransactionManager(
                hooks=load_hooks(LIBLPM_HOOK_DIRS),
                root=root,
                base_env={"LPM_ROOT": str(root)},
                failure_mode=hook_failure_mode,
            )
            owns_txn = True

        if name in PROTECTED and not force:
            warn(f"{name} is protected (from {PROTECTED_FILE}) and cannot be removed without --force")
            return

        conn = db()
        cur = conn.execute(
            "SELECT version, release, manifest, requires, explicit FROM installed WHERE name=?",
            (name,),
        )
        row = cur.fetchone()
        if not row:
            warn(f"{name} not installed")
            return

        version, release, manifest_json, requires_json, explicit_int = row
        manifest = json.loads(manifest_json) if manifest_json else []
        requires = json.loads(requires_json) if requires_json else []
        explicit = bool(explicit_int)
        meta = {
            "name": name,
            "version": version,
            "release": release,
            "manifest": manifest,
            "requires": requires,
            "explicit": explicit,
        }

        manifest_paths = _normalize_manifest_paths(manifest)
        is_meta_package = not manifest_paths and bool(requires)
        meta_requires = requires

        if txn is not None and register_event and not dry_run:
            txn.add_package_event(
                name=name,
                operation="Remove",
                version=version,
                release=release,
                paths=manifest_paths,
            )

        if txn is not None and not dry_run:
            txn.ensure_pre_transaction()

        with transaction(conn, f"remove {name}", dry_run):
            run_hook("pre_remove", {"LPM_PKG": name, "LPM_ROOT": str(root)}, failure_mode=hook_failure_mode, package_context=name)
            _remove_installed_package(meta, root, dry_run, conn)
            run_hook("post_remove", {"LPM_PKG": name, "LPM_ROOT": str(root)}, failure_mode=hook_failure_mode, package_context=name)

        if not dry_run and is_meta_package:
            installed_after = db_installed(conn)
            needed = _compute_needed_set(installed_after)
            candidates = {req.split()[0] for req in meta_requires}
            auto_remove = []
            for cand in sorted(candidates):
                dep_meta = installed_after.get(cand)
                if not dep_meta:
                    continue
                if dep_meta.get("explicit"):
                    continue
                if cand in needed:
                    continue
                auto_remove.append(cand)

            if auto_remove:
                log(
                    f"[lpm] Autoremoving dependencies no longer needed after {name}: "
                    + ", ".join(auto_remove)
                )
                for dep in auto_remove:
                    removepkg(
                        name=dep,
                        root=root,
                        dry_run=dry_run,
                        force=force,
                        hook_transaction=txn,
                        register_event=register_event,
                    )

        if txn is not None and owns_txn and not dry_run:
            txn.run_post_transaction()

        ok(f"Removed {name}-{version}")

    
def cmd_protected(a):
    current = load_protected()
    if a.action == "list":
        print(json.dumps({"protected": current}, indent=2))
    elif a.action == "add":
        changed = False
        for n in a.names:
            if n not in current:
                current.append(n)
                changed = True
        if changed:
            with operation_phase(privileged=True):
                write_json(PROTECTED_FILE, {"protected": sorted(current)})
            ok("Updated protected list")
        else:
            log("No changes")
    elif a.action == "remove":
        new = [n for n in current if n not in a.names]
        if new != current:
            with operation_phase(privileged=True):
                write_json(PROTECTED_FILE, {"protected": sorted(new)})
            ok("Updated protected list")
        else:
            log("No changes")


def cmd_setup(_):
    with operation_phase(privileged=True):
        _resolve_lpm_attr("run_first_run_wizard", run_first_run_wizard)()
        _resolve_lpm_attr("initialize_state", initialize_state)()


# =========================== Maintainer spec generation =======================
def _serialize_cli_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        return [_serialize_cli_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _serialize_cli_value(v) for k, v in value.items()}
    return str(value)


def _action_type_name(action: argparse.Action) -> Optional[str]:
    action_type = getattr(action, "type", None)
    if action_type is None:
        return None
    if isinstance(action_type, type):
        return action_type.__name__
    return getattr(action_type, "__name__", repr(action_type))


def _action_to_spec(action: argparse.Action) -> Optional[dict[str, Any]]:
    if isinstance(action, argparse._HelpAction):
        return None
    if isinstance(action, argparse._SubParsersAction):
        return None

    flags = list(action.option_strings)
    spec: dict[str, Any] = {
        "name": action.dest,
        "flags": flags if flags else [action.dest],
        "help": action.help or "",
        "positional": not bool(flags),
    }

    if getattr(action, "required", False):
        spec["required"] = True
    if action.metavar is not None:
        spec["metavar"] = action.metavar
    if action.nargs is not None and action.nargs != 1:
        spec["nargs"] = action.nargs
    if action.choices is not None:
        spec["choices"] = [_serialize_cli_value(choice) for choice in action.choices]
    if action.default is not argparse.SUPPRESS:
        spec["default"] = _serialize_cli_value(action.default)

    type_name = _action_type_name(action)
    if type_name:
        spec["type"] = type_name

    return spec


def _build_cli_spec(parser: argparse.ArgumentParser) -> dict[str, Any]:
    cli_spec: dict[str, Any] = {
        "usage": parser.format_usage().strip(),
        "description": parser.description or "",
        "arguments": [],
        "commands": [],
    }

    subparsers_action: Optional[argparse._SubParsersAction] = None
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            subparsers_action = action
            continue
        spec = _action_to_spec(action)
        if spec:
            cli_spec["arguments"].append(spec)

    if subparsers_action is None:
        return cli_spec

    help_lookup = {
        choice.dest: choice.help or ""
        for choice in getattr(subparsers_action, "_choices_actions", [])
    }

    commands: list[dict[str, Any]] = []
    for name, subparser in sorted(subparsers_action.choices.items()):
        cmd_spec = {
            "name": name,
            "help": help_lookup.get(name, ""),
            "usage": subparser.format_usage().strip(),
            "description": subparser.description or "",
            "arguments": [],
        }
        for action in subparser._actions:
            if isinstance(action, argparse._SubParsersAction):
                continue
            spec = _action_to_spec(action)
            if spec:
                cmd_spec["arguments"].append(spec)
        commands.append(cmd_spec)

    cli_spec["commands"] = commands
    return cli_spec


def _build_lpmspec(parser: argparse.ArgumentParser) -> dict[str, Any]:
    return {
        "api_version": LPMSPEC_API_VERSION,
        "generated_at": int(time.time()),
        "lpm": get_runtime_metadata(),
        "cli": _build_cli_spec(parser),
    }


def cmd_generate_lpmspec(args) -> None:
    if not maintainer_mode.is_enabled():
        die("lpmspec generation requires distro maintainer mode")

    parser = build_parser()
    spec = _build_lpmspec(parser)

    output_path = Path(args.output) if args.output else _config.DISTRO_LPMSPEC_PATH
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.output is None:
        _config.DISTRO_LPMSPEC_PATH = output_path
    ok(f"Generated lpmspec at {output_path}")


# =========================== Argparse / main ==================================
def _run_sysconfig(root: Path) -> None:
    from .sysconfig import apply_system_configuration

    results = apply_system_configuration(root)
    created = updated = unchanged = skipped = 0
    errors = []
    for result in results:
        path = result.path
        action = result.action
        if action in {"created", "updated"}:
            if action == "created":
                created += 1
            else:
                updated += 1
            ok(f"[sysconfig] {action}: {path}")
        elif action == "unchanged":
            unchanged += 1
            log(f"[sysconfig] unchanged: {path}")
        elif action == "skipped":
            skipped += 1
            warn(f"[sysconfig] skipped {path}: {result.message}")
        elif action == "error":
            errors.append(result)
            warn(f"[sysconfig] error {path}: {result.message}")

    if errors:
        details = "; ".join(f"{res.path}: {res.message}" for res in errors)
        die(f"failed to apply system configuration: {details}")

    ok(
        "system configuration prepared"
        f" (created={created}, updated={updated}, unchanged={unchanged}, skipped={skipped})"
    )




def cmd_bootstrap(args)->int:
    return bootstrap.run_bootstrap(args)


def cmd_bootstrap_chroot(args) -> int:
    return chroot_helpers.run_bootstrap_chroot(args)


def cmd_installroot(args) -> int:
    return chroot_helpers.run_installroot(args)


def cmd_buildgen(args) -> int:
    return chroot_helpers.run_buildgen(args)


def cmd_buildchroot(args) -> int:
    return chroot_helpers.run_buildchroot(args)


def build_parser()->argparse.ArgumentParser:
    p=argparse.ArgumentParser(prog="lpm", description="Linux Package Manager with SAT solver, signatures, and .lpmbuild")
    p.add_argument(
        "--sysconfig",
        action="store_true",
        help="provision baseline system configuration files and exit",
    )
    p.add_argument(
        "--sysconfig-root",
        type=Path,
        default=Path("/"),
        help="filesystem root used when generating system configuration files",
    )
    sub=p.add_subparsers(dest="cmd")

    sp=sub.add_parser("setup", help="Run the interactive configuration wizard"); sp.set_defaults(func=cmd_setup)
    sp=sub.add_parser("repolist", help="Show configured repositories"); sp.set_defaults(func=cmd_repolist)
    sp=sub.add_parser("repoadd", help="Add a repository"); sp.add_argument("name"); sp.add_argument("url");                   sp.add_argument("--priority",type=int,default=10); sp.set_defaults(func=cmd_repoadd)
    sp=sub.add_parser("repodel", help="Remove a repository"); sp.add_argument("name"); sp.set_defaults(func=cmd_repodel)

    sp=sub.add_parser("clean", help="Remove cached blobs"); sp.set_defaults(func=cmd_clean_cache)

    sp=sub.add_parser("search", help="Search packages"); sp.add_argument("patterns", nargs="*"); sp.set_defaults(func=cmd_search)
    sp=sub.add_parser("info", help="Show package info"); sp.add_argument("names", nargs="+"); sp.set_defaults(func=cmd_info)

    sp=sub.add_parser("install", help="Install packages")
    sp.add_argument("names", nargs="+")
    sp.add_argument("--root")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--no-verify", action="store_true", help="skip signature verification (DANGEROUS)")
    sp.add_argument(
        "--no-delta",
        action="store_true",
        help="disable use of delta packages (overrides configuration)",
    )
    sp.add_argument(
        "--allow-fallback",
        dest="allow_fallback",
        action="store_true",
        help="enable GitLab .lpmbuild fallback when repository fetches fail",
    )
    sp.add_argument(
        "--no-fallback",
        dest="allow_fallback",
        action="store_false",
        help="disable GitLab .lpmbuild fallback (overrides configuration)",
    )
    sp.set_defaults(func=cmd_install, allow_fallback=None)

    sp=sub.add_parser("remove", help="Remove packages")
    sp.add_argument("names", nargs="+")
    sp.add_argument("--root")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--force", action="store_true", help="override protected package list")
    sp.set_defaults(func=cmd_remove)

    sp=sub.add_parser("autoremove", help="Remove unneeded packages")
    sp.add_argument("--root")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_autoremove)

    def add_upgrade_subparser(name: str, help_text: str):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("names", nargs="*")
        sp.add_argument("--root")
        sp.add_argument("--dry-run", action="store_true")
        sp.add_argument("--no-verify", action="store_true", help="skip signature verification (DANGEROUS)")
        sp.add_argument(
            "--no-delta",
            action="store_true",
            help="disable use of delta packages (overrides configuration)",
        )
        sp.add_argument(
            "--allow-fallback",
            dest="allow_fallback",
            action="store_true",
            help="enable GitLab .lpmbuild fallback when the resolver cannot find packages",
        )
        sp.add_argument(
            "--no-fallback",
            dest="allow_fallback",
            action="store_false",
            help="disable GitLab .lpmbuild fallback (overrides configuration)",
        )
        sp.add_argument("--force", action="store_true", help="override protected package list for install/upgrade")
        sp.set_defaults(func=cmd_upgrade, allow_fallback=None)
        return sp

    add_upgrade_subparser("upgrade", "Upgrade packages (targets or all)")
    add_upgrade_subparser("upgradepkg", "Alias for upgrade; upgrade packages (targets or all)")

    sp=sub.add_parser("list", help="List installed packages"); sp.set_defaults(func=cmd_list_installed)
    sp=sub.add_parser("files", help="List files installed by package"); sp.add_argument("name"); sp.set_defaults(func=cmd_files)
    sp=sub.add_parser("snapshots", help="List snapshots"); sp.add_argument("--delete", type=int, nargs="*", help="snapshot IDs to delete"); sp.add_argument("--prune", action="store_true", help="prune old snapshots"); sp.set_defaults(func=cmd_snapshots)
    sp=sub.add_parser("rollback", help="Restore from snapshot"); sp.add_argument("snapshot_id", nargs="?", type=int, help="snapshot ID (default latest)"); sp.set_defaults(func=cmd_rollback)
    sp=sub.add_parser("history", help="Show last transactions"); sp.set_defaults(func=cmd_history)
    sp=sub.add_parser("verify", help="Verify installed files exist"); sp.add_argument("--root"); sp.set_defaults(func=cmd_verify)

    sp=sub.add_parser("pins", help="Show or set holds/preferences")
    sp.add_argument("action", choices=["list","hold","unhold","prefer"])
    sp.add_argument("names", nargs="*", help="for hold/unhold")
    sp.add_argument("--prefs", nargs="*", default=[], help="name:constraint for prefer")
    sp.set_defaults(func=cmd_pins)

    sp=sub.add_parser("build", help=f"Build a {EXT} package from a staged root (DESTDIR)")
    sp.add_argument("stagedir", help="directory with staged files")
    sp.add_argument("--name", required=True)
    sp.add_argument("--version", required=True)
    sp.add_argument("--release", default="1")
    sp.add_argument("--arch", default=ARCH)
    sp.add_argument("--summary", default="")
    sp.add_argument("--url", default="")
    sp.add_argument("--license", default="")
    sp.add_argument("--developer", default="")
    sp.add_argument("--requires", nargs="*", default=[])
    sp.add_argument("--provides", nargs="*", default=[])
    sp.add_argument("--conflicts", nargs="*", default=[])
    sp.add_argument("--obsoletes", nargs="*", default=[])
    sp.add_argument("--recommends", nargs="*", default=[])
    sp.add_argument("--suggests", nargs="*", default=[])
    sp.add_argument("--output", help=f"output {EXT} file")
    sp.add_argument("--no-sign", action="store_true", help="do not sign even if key exists")
    sp.add_argument("--install-default", choices=["y", "n"], help="default answer for install prompt")
    sp.set_defaults(func=cmd_build)

    sp=sub.add_parser("splitpkg", help=f"Package an additional staged root during .lpmbuild execution")
    sp.add_argument("--stagedir", required=True, type=Path, help="directory containing files for the split package")
    sp.add_argument("--name", help="name of the split package (defaults to base NAME)")
    sp.add_argument("--version", help="override version (defaults to base VERSION)")
    sp.add_argument("--release", help="override release (defaults to base RELEASE)")
    sp.add_argument("--arch", help="override architecture (defaults to base ARCH)")
    sp.add_argument("--summary", help="package summary")
    sp.add_argument("--url", help="homepage URL")
    sp.add_argument("--license", help="license identifier")
    sp.add_argument("--developer", help="package developer/maintainer")
    sp.add_argument("--requires", action="append", help="dependency (can be repeated)")
    sp.add_argument("--provides", action="append", help="virtual provide (can be repeated)")
    sp.add_argument("--conflicts", action="append", help="conflicting package (can be repeated)")
    sp.add_argument("--obsoletes", action="append", help="obsoleted package (can be repeated)")
    sp.add_argument("--recommends", action="append", help="recommended dependency (can be repeated)")
    sp.add_argument("--suggests", action="append", help="suggested dependency (can be repeated)")
    sp.add_argument("--outdir", type=Path, help="directory for built split packages")
    sp.add_argument("--output", type=Path, help=f"explicit output {EXT} path")
    sp.add_argument("--no-sign", action="store_true", help="do not sign even if key exists")
    sp.set_defaults(func=cmd_splitpkg)

    sp=sub.add_parser("buildpkg", help=f"Build a {EXT} package from a .lpmbuild script")
    sp.add_argument("script", nargs="?", type=Path)
    sp.add_argument(
        "overrides",
        nargs="*",
        metavar="@Override=...",
        help="override CPU tuning, e.g. @Override=\"arch=x86_64v3 -march=x86_64v3 -mtune=generic\"",
    )
    sp.add_argument("--outdir", default=Path.cwd(), type=Path)
    sp.add_argument("--no-deps", action="store_true", help="do not fetch or build dependencies")
    sp.add_argument(
        "--force-rebuild",
        action="store_true",
        help="rebuild the target package and all dependencies even if already available",
    )
    sp.add_argument("--install-default", choices=["y", "n"], help="default answer for install prompt")
    sp.add_argument("--python-pip", metavar="SPEC", help="build a package from a Python distribution fetched via pip")
    sp.set_defaults(func=cmd_buildpkg)

    sp=sub.add_parser("rebuild", help="Rebuild an installed package and all installed reverse dependencies")
    sp.add_argument("name", help="installed package name to rebuild from")
    sp.add_argument("--outdir", default=Path.cwd(), type=Path)
    sp.add_argument("--no-deps", action="store_true", help="do not fetch or build dependencies")
    sp.add_argument("--install-default", choices=["y", "n"], help="default answer for install prompt")
    sp.add_argument(
        "--cycle-policy",
        choices=["fail", "group"],
        default="fail",
        help="how to handle reverse-dependency cycles (default: fail)",
    )
    sp.add_argument(
        "--force-rebuild",
        action="store_true",
        default=True,
        help="rebuild the target package and all reverse dependencies even if already available",
    )
    sp.add_argument(
        "--conflict-policy",
        choices=["fail", "skip"],
        default="fail",
        help="how to handle rebuild target conflicts detected during preflight (default: fail)",
    )
    sp.set_defaults(func=cmd_rebuild)

    sp=sub.add_parser("genindex", help=f"Generate index.json for a repo directory of {EXT} files")
    sp.add_argument("repo_dir", help=f"directory containing {EXT} files")
    sp.add_argument("--base-url", dest="base_url", help="base URL for blobs in index (e.g., https://repo.example.com)", default=None)
    sp.add_argument("--arch", help="only include this arch (noarch always included)", default=None)
    sp.set_defaults(func=cmd_genindex)

    sp=sub.add_parser("createiso", help="Create a GRUB bootable live ISO from a populated target root")
    sp.add_argument("--source-root", default="/", help="filesystem root to package (default: /)")
    sp.add_argument("--output", required=True, help="output .iso file path")
    sp.add_argument("--volume-id", default="LPM_LIVE", help="ISO volume identifier")
    sp.add_argument("--architecture", choices=["x86_64", "x86_64-v2"], default="x86_64-v2")
    sp.add_argument("--kernel", help="kernel path, absolute or relative to source root")
    sp.add_argument("--initramfs", help="initramfs path, absolute or relative to source root")
    sp.add_argument("--staging-root", help="retain/use a specific ISO staging directory")
    sp.add_argument("--dry-run", action="store_true", help="validate and print the ISO build plan")
    sp.set_defaults(func=cmd_createiso)

    sp=sub.add_parser("systemiso", help="Build a complete source-based target root and bootable live ISO")
    sp.add_argument("--lpmbuild-root", required=True, help="root of the package recipe repository")
    sp.add_argument("--package-profile", required=True, help="newline-delimited live-system package profile")
    sp.add_argument("--root", required=True, help="target root directory")
    sp.add_argument("--output", required=True, help="output ISO path")
    sp.add_argument("--artifact-dir", help="directory for built LPM packages")
    sp.add_argument("--iso-staging", help="persistent ISO staging directory")
    sp.add_argument("--architecture", choices=["x86_64", "x86_64-v2"], default="x86_64-v2")
    sp.add_argument("--hostname", default="lpm-live")
    sp.add_argument("--volume-id", default="LPM_LIVE")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_systemiso)

    if maintainer_mode.is_enabled():
        sp=sub.add_parser("lpmspec", help="Generate an lpmspec description for Nebula installers")
        sp.add_argument(
            "--output",
            type=Path,
            help="destination path for the generated lpmspec JSON (defaults to distro maintainer path)",
        )
        sp.set_defaults(func=cmd_generate_lpmspec)

    sp=sub.add_parser("installpkg", help=f"Install from local {EXT} file(s)")
    sp.add_argument("files", nargs="+", help=f"{EXT} package file(s) to install")
    sp.add_argument("--root")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--verify", action="store_true", help="verify .sig with trusted keys")
    sp.add_argument("--force", action="store_true", help="override protected package list for install/upgrade")
    sp.set_defaults(func=cmd_fileinstall)


    sp=sub.add_parser("removepkg", help="Remove installed package(s)")
    sp.add_argument("names", nargs="+", help="package name(s) to remove")
    sp.add_argument("--root")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--force", action="store_true", help="override protected package list")
    sp.set_defaults(func=cmd_fileremove)

    sp=sub.add_parser("bootstrap", help="Bootstrap a new target system")
    sp.add_argument("--target")
    sp.add_argument("--architecture", choices=["x86_64", "x86_64-v2"], default=None)
    sp.add_argument("--initramfs-tool", choices=["mkinitcpio", "dracut"], default=None)
    sp.add_argument("--hostname")
    sp.add_argument("--timezone")
    sp.add_argument("--locale")
    sp.add_argument("--keymap")
    sp.add_argument("--bootloader")
    sp.add_argument("--kernel")
    sp.add_argument("--config")
    sp.add_argument("--resume", action="store_true", default=None)
    sp.add_argument("--dry-run", action="store_true", default=None)
    sp.add_argument("--verbose", action="store_true", default=None)
    sp.add_argument("--force", action="store_true", default=None)
    sp.add_argument("--efi-dir")
    sp.add_argument("--boot-device")
    sp.add_argument("--network")
    sp.add_argument("--plan-file", help="JSON package-order manifest")
    sp.add_argument("--lpmbuild-root", help="build and install all local .lpmbuild recipes")
    sp.add_argument("--source-output", help="directory for source-built package artifacts")
    sp.add_argument("--include-packages", help="comma-separated source packages to include")
    sp.add_argument("--package-profile", help="newline-delimited package selection profile")
    sp.add_argument("--exclude-packages", help="comma-separated source packages to exclude")
    sp.add_argument("--partition-plan", help="validated JSON disk layout")
    sp.add_argument(
        "--partition-confirm",
        action="store_true",
        default=None,
        help="confirm destructive partition-table and filesystem creation",
    )
    sp.set_defaults(func=cmd_bootstrap)

    sp=sub.add_parser("bootstrap-chroot", help="Bootstrap a chroot target root")
    sp.add_argument("--root", required=True, help="target root path")
    sp.add_argument("--package", dest="packages", action="append", default=[], help="package name (repeatable)")
    sp.add_argument("--manifest", help="path to package manifest input")
    sp.add_argument("--cache-dir", default=CACHE_DIR, help="package cache directory")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=cmd_bootstrap_chroot)

    sp=sub.add_parser("installroot", help="Install packages into a target root")
    sp.add_argument("--root", required=True, help="target root path")
    sp.add_argument("--package", dest="packages", action="append", default=[], help="package name (repeatable)")
    sp.add_argument("--manifest", help="path to package manifest input")
    sp.add_argument("--cache-dir", default=CACHE_DIR, help="package cache directory")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--mount-api", action="store_true", help="mount /proc,/sys,/dev in target root during install")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=cmd_installroot)

    sp=sub.add_parser("buildgen", help="Generate build artifacts for chroot builds")
    sp.add_argument("--root", required=True, help="target root path")
    sp.add_argument("--source", required=True, help="path to .lpmbuild source")
    sp.add_argument("--output-dir", default=str(Path.cwd()), help="output directory")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=cmd_buildgen)

    sp=sub.add_parser("buildchroot", help="Build package(s) within a target chroot")
    sp.add_argument("--root", required=True, help="target root path")
    sp.add_argument("--source", required=True, help="path to .lpmbuild source")
    sp.add_argument("--cache-dir", default=CACHE_DIR, help="package cache directory")
    sp.add_argument("--output-dir", default=str(Path.cwd()), help="output directory")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--verbose", action="store_true")
    sp.set_defaults(func=cmd_buildchroot)

    sp = sub.add_parser("protected", help="Show or edit protected package list")
    sp.add_argument("action", choices=["list", "add", "remove"])
    sp.add_argument("names", nargs="*", help="package names (for add/remove)")
    sp.set_defaults(func=cmd_protected)


    return p

def main(argv=None):
    parser=build_parser()
    args=parser.parse_args(argv)
    cmd = getattr(args, "cmd", None)
    if args.sysconfig:
        if cmd:
            parser.error("--sysconfig cannot be combined with subcommands")
        _run_sysconfig(args.sysconfig_root)
        return 0
    if cmd is None:
        parser.error("a subcommand is required")
    conf_file = _resolve_lpm_attr("CONF_FILE", CONF_FILE)
    try:
        if cmd != "setup" and not conf_file.exists():
            with operation_phase(privileged=True):
                _resolve_lpm_attr("run_first_run_wizard", run_first_run_wizard)()
                _resolve_lpm_attr("initialize_state", initialize_state)()
        elif cmd in _STATE_COMMANDS:
            _initialize_cli_state()
        if cmd in _PRIVILEGED_COMMANDS:
            with operation_phase(privileged=True):
                require_root(cmd)
                args.func(args)
        else:
            args.func(args)
    except FirstRunSetupError as e:
        die(str(e))
    except PermissionError as e:
        die(_state_setup_permission_message(e))
    except ResolutionError as e:
        die(f"dependency resolution failed: {e}")

if __name__=="__main__":
    main()
