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
import argparse, contextlib, dataclasses, fnmatch, hashlib, io, json, os, re, shlex, shutil, sqlite3, stat, subprocess, sys, tarfile, tempfile, time, urllib.parse
from email.parser import Parser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Iterable, Callable
from collections import deque
import zstandard as zstd
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import Specifier, SpecifierSet
from packaging.utils import canonicalize_name

# =========================== Runtime metadata =================================
_ENV_NAME = "LPM_NAME"
_ENV_VERSION = "LPM_VERSION"
_ENV_BUILD = "LPM_BUILD"
_ENV_BUILD_DATE = "LPM_BUILD_DATE"
_ENV_DEVELOPER = "LPM_DEVELOPER"
_ENV_URL = "LPM_URL"

_DEFAULT_NAME = "LPM"
_DEFAULT_VERSION = "0.9.19.25"
_DEFAULT_BUILD = "development"
_DEFAULT_BUILD_DATE = ""
_DEFAULT_DEVELOPER = "Derek Midkiff aka BobTheZombie"
_DEFAULT_URL = "https://github.com/BobTheZombie/LPM"

__title__ = os.environ.get(_ENV_NAME, _DEFAULT_NAME)
__version__ = os.environ.get(_ENV_VERSION, _DEFAULT_VERSION)
__build__ = os.environ.get(_ENV_BUILD, _DEFAULT_BUILD)
__build_date__ = os.environ.get(_ENV_BUILD_DATE, _DEFAULT_BUILD_DATE)
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

    return {
        "name": __title__,
        "version": __version__,
        "build": __build__,
        "build_date": __build_date__,
        "developer": __developer__,
        "url": __url__,
    }

from src.config import (
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
    HOOK_DIR,
    LIBLPM_HOOK_DIRS,
    MAX_LEARNT_CLAUSES,
    INSTALL_PROMPT_DEFAULT,
    MAX_SNAPSHOTS,
    MARCH,
    MTUNE,
    OPT_LEVEL,
    PIN_FILE,
    REPO_LIST,
    SIGN_KEY,
    SNAPSHOT_DIR,
    TRUST_DIR,
    detect_init_system,
    initialize_state,
)
initialize_state()
from src.fs import read_json, write_json, urlread
from src.installgen import generate_install_script
from src.solver import CNF, CDCLSolver
from src.first_run_ui import run_first_run_wizard
from src.liblpmhooks import HookTransactionManager, load_hooks

# =========================== Protected packages ===============================
PROTECTED_FILE = Path("/etc/lpm/protected.json")

def load_protected() -> List[str]:
    default = ["glibc", "zlib", "lpm"]
    if not PROTECTED_FILE.exists():
        try:
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

def log(msg: str):
    print(f"{PURPLE}{msg}{RESET}", file=sys.stderr)

def die(msg: str, code: int = 2):
    print(f"{RED}[ERROR]{RESET} {msg}", file=sys.stderr)
    sys.exit(code)

def ok(msg: str):
    print(f"{GREEN}[OK]{RESET} {msg}", file=sys.stderr)

def warn(msg: str):
    print(f"{CYAN}[WARN]{RESET} {msg}", file=sys.stderr)

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

    return cls(
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
    wrapper = f"set -e\nsource {script_quoted}\n{wrapper_body}"

    if mode == "fakeroot":
        cmd = ["fakeroot", "bash", "-c", wrapper]
        subprocess.run(cmd, check=True, env=env, cwd=str(cwd))
        return

    if mode == "bwrap":
        # bwrap isolates FS: read-only root, only bind staging/build/src dirs
        cmd = [
            "bwrap",
            "--ro-bind", "/", "/",
            "--bind", str(stagedir), "/pkgdir",
            "--bind", str(buildroot), "/build",
            "--bind", str(srcroot), "/src",
            "--dev", "/dev",
            "--proc", "/proc",
            "--unshare-all",
            "--share-net",             # allow networking (remove for full isolation)
            "--die-with-parent",
            "bash", "-c", f"set -e\ncd /src\nsource {script_quoted}\n{wrapper_body}"
        ]
        subprocess.run(cmd, check=True, env=env, cwd=str(cwd))
        return

    # Default: no sandbox
    cmd = ["bash", "-c", wrapper]
    subprocess.run(cmd, check=True, env=env, cwd=str(cwd))

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
    return pkg_arch == "noarch" or pkg_arch == want_arch

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
        if name in ("|","||",",","(",")", None): raise ValueError("bad dep atom")
        op = ""
        if peek() in ("==","=","<=",">=","<",">","~","~="): op = eat()
        ver = ""
        if peek() in ("(",):
            eat("(")
            if peek() in ("==","=","<=",">=","<",">","~","~="): op = eat()
            ver = eat()
            eat(")")
        elif op:
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
    if e.kind!="and": return [e]
    return flatten_and(e.left) + flatten_and(e.right)

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
    requires: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)
    obsoletes: List[str] = field(default_factory=list)
    provides: List[str] = field(default_factory=list)
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
    @staticmethod
    def from_dict(d: dict, repo_name="(local)", prio=0, bias: float = 1.0, decay: float = 0.95) -> "PkgMeta":
        return PkgMeta(
            name=d["name"], version=d["version"], release=d.get("release","1"),
            arch=d.get("arch","noarch"), summary=d.get("summary",""), url=d.get("url",""),
            license=d.get("license",""), requires=d.get("requires",[]), conflicts=d.get("conflicts",[]),
            obsoletes=d.get("obsoletes",[]), provides=d.get("provides",[]), symbols=d.get("symbols",[]), recommends=d.get("recommends",[]),
            suggests=d.get("suggests",[]), size=d.get("size",0), sha256=d.get("sha256"), blob=d.get("blob"),
            repo=repo_name, prio=prio, bias=bias, decay=decay, kernel=d.get("kernel", False),
            mkinitcpio_preset=d.get("mkinitcpio_preset"))

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
    write_json(REPO_LIST, [dataclasses.asdict(r) for r in rs])

def add_repo(name,url,priority=10,bias=1.0,decay=0.95):
    rs=list_repos()
    if any(r.name==name for r in rs): die(f"repo {name} exists")
    rs.append(Repo(name,url,priority,bias,decay)); save_repos(rs); ok(f"Added repo {name}")

def del_repo(name):
    save_repos([r for r in list_repos() if r.name!=name]); ok(f"Removed repo {name}")

def fetch_repo_index(repo: Repo) -> List[PkgMeta]:
    idx_url = repo.url.rstrip("/") + "/index.json"
    raw, _ = urlread(idx_url)
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
def db() -> sqlite3.Connection:
    c = sqlite3.connect(str(DB_PATH))
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
    return c

def db_installed(conn) -> Dict[str,dict]:
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
    with archive.open("wb") as f:
        with cctx.stream_writer(f) as compressor:
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

def encode_resolution(u: Universe, goals: List[DepExpr]) -> Tuple[CNF, Dict[Tuple[str,str],int], Set[int], Set[int], Dict[int,float], Dict[int,float]]:
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
        for s in p.requires:
            if not s: continue
            e = parse_dep_expr(s)
            if e.kind=="and":
                for part in flatten_and(e):
                    disj = expr_to_cnf_disj(u, part, cnf, var_of)
                    cnf.add([-vp] + (disj or [])) if disj else cnf.add([-vp])
            else:
                disj = expr_to_cnf_disj(u, e, cnf, var_of)
                cnf.add([-vp] + (disj or [])) if disj else cnf.add([-vp])
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
    for g in goals:
        if g.kind=="and":
            for part in flatten_and(g):
                disj = expr_to_cnf_disj(u, part, cnf, var_of)
                if not disj:
                    raise ResolutionError("No provider for goal part")
                cnf.add(disj)
        else:
            disj = expr_to_cnf_disj(u, g, cnf, var_of)
            if not disj:
                raise ResolutionError("No provider for goal")
            cnf.add(disj)

    return cnf, var_of, prefer_true, prefer_false, bias_map, decay_map

def solve(goals: List[str], universe: Universe) -> List[PkgMeta]:
    goal_exprs = [parse_dep_expr(s) for s in goals]
    cnf, var_of, ptrue, pfalse, bias_map, decay_map = encode_resolution(universe, goal_exprs)
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
        raise ResolutionError(
            "Unsatisfiable dependency set involving: " + ", ".join(names)
        )
    chosen: Dict[str,PkgMeta] = {}
    for vid,val in res.assign.items():
        if not val: continue
        key = inv.get(vid); 
        if not key: continue
        name,ver = key
        for p in universe.candidates_by_name.get(name, []):
            if p.version==ver: chosen[name]=p; break
    # topo-ish order by requires depth
    chosen_names=set(chosen.keys()); dep_depth: Dict[str,int]={}
    def depth_of(p: PkgMeta)->int:
        if p.name in dep_depth: return dep_depth[p.name]
        d=0
        for s in p.requires:
            e=parse_dep_expr(s); parts=flatten_and(e) if e.kind=="and" else [e]
            for part in parts:
                if part.kind=="atom":
                    for q in providers_for(universe, part.atom):
                        if q.name in chosen_names:
                            d=max(d, 1+depth_of(chosen[q.name]))
        dep_depth[p.name]=d; return d
    return sorted(chosen.values(), key=lambda p: depth_of(p))

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


def _run_hook_script(script: Path, env: Dict[str, str]):
    merged_env = {**os.environ, **env}
    if os.access(script, os.X_OK):
        subprocess.run([str(script)], env=merged_env, check=True)
        return

    if script.suffix == ".py":
        interpreter = _detect_python_for_hooks()
        if interpreter:
            subprocess.run([interpreter, str(script)], env=merged_env, check=True)
            return

        shebang_cmd = _shebang_command(script)
        if shebang_cmd:
            subprocess.run([*shebang_cmd, str(script)], env=merged_env, check=True)
            return

        raise RuntimeError(f"Unable to locate Python interpreter for hook {script}")

    shebang_cmd = _shebang_command(script)
    if shebang_cmd:
        subprocess.run([*shebang_cmd, str(script)], env=merged_env, check=True)


def run_hook(hook: str, env: Dict[str,str]):
    path = HOOK_DIR / hook
    if path.is_file():
        _run_hook_script(path, env)

    py_path = path.with_suffix(".py")
    if py_path.is_file():
        _run_hook_script(py_path, env)

    dpath = HOOK_DIR / f"{hook}.d"
    if dpath.is_dir():
        for script in sorted(dpath.iterdir()):
            if script.is_file():
                _run_hook_script(script, env)
        
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


def handle_service_files(pkg_name: str, root: Path, manifest_entries: Optional[List[object]] = None):
    """
    Detect service files from installed package and register them
    according to the active init system.
    """
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

    for root, _, files in os.walk(stagedir):
        root_path = Path(root)
        for fn in files:
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


def _collect_python_package_metadata(stagedir: Path, *, include_requires_dist: bool) -> Dict[str, object]:
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
    arch = _detect_python_package_arch(stagedir)
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

        env = os.environ.copy()
        env.setdefault("PYTHONNOUSERSITE", "1")

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

            info = _collect_python_package_metadata(stagedir, include_requires_dist=include_deps)
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
            build_package(stagedir, meta, out, sign=True)

            duration = time.time() - start
            return out, meta, duration

    if last_error:
        raise last_error
    raise RuntimeError(f"pip build: unable to download or build requirement '{spec}'")


# =========================== Unified package tar opener =========================
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

    dctx = zstd.ZstdDecompressor()

    if stream:
        # Stream directly into tarfile (used for extraction)
        f = blob.open("rb")
        reader = dctx.stream_reader(f)
        return tarfile.open(fileobj=reader, mode="r|")
    else:
        # Buffer the decompression into memory for random-access tarfile
        f = blob.open("rb")
        reader = dctx.stream_reader(f)
        buf = io.BytesIO()
        while True:
            chunk = reader.read(16384)
            if not chunk:
                break
            buf.write(chunk)
        buf.seek(0)
        return tarfile.open(fileobj=buf, mode="r:")


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

    with meta_path.open("w", encoding="utf-8") as f:
        json.dump(meta_dict, f, indent=2, sort_keys=True)
    with mani_path.open("w", encoding="utf-8") as f:
        json.dump(mani, f, indent=2)

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
    if sign and SIGN_KEY.exists():
        sig = out.with_suffix(out.suffix + ".sig")
        subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", str(SIGN_KEY),
             "-out", str(sig), str(out)],
            check=True
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
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                tf.extract(m, path=str(root), filter="data")
            manifest.append("/" + rel)

    return manifest


def _cache_path_for(url: str) -> Path:
    name = os.path.basename(urllib.parse.urlparse(url).path) or f"lpm{EXT}"
    return CACHE_DIR / name

def fetch_blob(p: PkgMeta) -> Tuple[Path, Optional[Path]]:
    if not p.blob: die(f"{p.name}-{p.version} missing blob")
    url=p.blob
    dst = _cache_path_for(url)
    sig_dst = dst.with_suffix(dst.suffix + ".sig")
    # local file:// or absolute
    if url.startswith("file://"):
        src = Path(url[7:])
        if not src.exists(): die(f"blob not found {src}")
        if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime: shutil.copy2(src, dst)
        sig_src = src.with_suffix(src.suffix + ".sig")
        if sig_src.exists(): shutil.copy2(sig_src, sig_dst)
    elif url.startswith("/") and Path(url).exists():
        src = Path(url)
        if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime: shutil.copy2(src, dst)
        sig_src = src.with_suffix(src.suffix + ".sig")
        if sig_src.exists(): shutil.copy2(sig_src, sig_dst)
    else:
        for _ in progress_bar(range(1), desc=f"Downloading {p.name}"):
            data, _ = urlread(url)
            dst.write_bytes(data)
        try:
            sig_url = url + ".sig"
            sig_data, _ = urlread(sig_url)
            sig_dst.write_bytes(sig_data)
        except Exception:
            pass
    return dst, (sig_dst if sig_dst.exists() else None)


def fetch_all(pkgs: List[PkgMeta]) -> Dict[str, object]:
    """Fetch all package blobs concurrently."""
    results: Dict[str, object] = {}
    if not pkgs:
        return results
    max_workers = min(8, len(pkgs))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_map = {ex.submit(fetch_blob, p): p.name for p in pkgs}
        for fut in progress_bar(
            as_completed(future_map),
            total=len(future_map),
            desc="Fetching",
            unit="pkg",
        ):
            name = future_map[fut]
            try:
                results[name] = fut.result()
            except Exception as e:
                results[name] = e
    return results

@contextlib.contextmanager
def transaction(conn: sqlite3.Connection, action: str, dry: bool):
    log(f"[tx] {action}{' (dry-run)' if dry else ''}")
    try:
        if not dry: conn.execute("BEGIN")
        yield
        if not dry: conn.execute("COMMIT"); ok(f"[tx] commit {action}")
    except Exception as e:
        if not dry: conn.execute("ROLLBACK")
        die(f"[tx] rollback {action}: {e}")

def do_install(
    pkgs: List[PkgMeta],
    root: Path,
    dry: bool,
    verify: bool,
    force: bool = False,
    explicit: Optional[Set[str]] = None,
    allow_fallback: bool = ALLOW_LPMBUILD_FALLBACK,
):
    global PROTECTED
    PROTECTED = load_protected()

    root = Path(root)

    explicit = set(explicit or [])

    to_fetch = [p for p in pkgs if not (p.name in PROTECTED and not force)]
    downloads = fetch_all(to_fetch)

    hook_txn: Optional[HookTransactionManager] = None
    installed_state: Dict[str, dict] = {}
    if not dry:
        hook_txn = HookTransactionManager(
            hooks=load_hooks(LIBLPM_HOOK_DIRS),
            root=root,
            base_env={"LPM_ROOT": str(root)},
        )
        conn = db()
        try:
            installed_state = db_installed(conn)
        finally:
            conn.close()

    jobs: List[Tuple[PkgMeta, Path]] = []
    for pkg in pkgs:
        if pkg.name in PROTECTED and not force:
            warn(f"{pkg.name} is protected (from {PROTECTED_FILE}) and cannot be installed/upgraded without --force")
            continue
        res = downloads.get(pkg.name)
        if isinstance(res, Exception):
            if not allow_fallback:
                die(
                    f"Failed to fetch {pkg.name}: {res}. GitLab fallback is disabled. "
                    "Re-run with --allow-fallback or set ALLOW_LPMBUILD_FALLBACK=1 in lpm.conf"
                )
            warn(f"Could not fetch {pkg.name} from repos ({res}), trying GitLab fallback...")
            tmp = Path(f"/tmp/lpm-dep-{pkg.name}.lpmbuild")
            fetch_lpmbuild(pkg.name, tmp)
            blob_path, _, _, _ = run_lpmbuild(tmp, prompt_install=False, is_dep=True, build_deps=True)
        else:
            if res is None:
                blob_path, _ = fetch_blob(pkg)
            else:
                blob_path = res[0]
        jobs.append((pkg, blob_path))

        if hook_txn is not None:
            meta, mani = read_package_meta(blob_path)
            manifest_paths = _normalize_manifest_paths(mani)
            operation = "Upgrade" if pkg.name in installed_state else "Install"
            hook_txn.add_package_event(
                name=meta.name,
                operation=operation,
                version=meta.version,
                release=meta.release,
                paths=manifest_paths,
            )

    if hook_txn is not None:
        hook_txn.ensure_pre_transaction()

    for pkg, blob_path in progress_bar(jobs, desc="Installing", unit="pkg"):
        try:
            meta = installpkg(
                blob_path,
                root=root,
                dry_run=dry,
                verify=verify,
                force=force,
                explicit=(pkg.name in explicit),
                allow_fallback=allow_fallback,
                hook_transaction=hook_txn,
                register_event=(hook_txn is None),
            )
        except Exception as e:
            warn(f"install {pkg.name}: {e}")
            continue

        if not dry and meta and getattr(meta, "kernel", False):
            run_hook(
                "kernel_install",
                {
                    "LPM_PKG": meta.name,
                    "LPM_VERSION": meta.version,
                    "LPM_PRESET": meta.mkinitcpio_preset or "",
                },
            )

    if hook_txn is not None:
        hook_txn.run_post_transaction()


def _remove_installed_package(meta: dict, root: Path, dry_run: bool, conn):
    """Remove files listed in manifest and update installed DB/history."""
    if dry_run:
        return
    name = meta["name"]
    manifest_entries = meta.get("manifest", [])
    # Handle both old list-of-paths and new structured manifest
    if manifest_entries and isinstance(manifest_entries[0], dict):
        files = [e["path"] for e in manifest_entries]
    else:
        files = manifest_entries

    # Remove deepest paths first (dirs last)
    files = sorted(files, key=lambda s: s.count("/"), reverse=True)

    for f in progress_bar(files, desc=f"Removing {name}", unit="file", colour="purple"):
        p = root / f.lstrip("/")
        try:
            if p.is_file() or p.is_symlink():
                p.unlink(missing_ok=True)
            elif p.is_dir():
                try:
                    p.rmdir()
                except OSError:
                    pass
        except Exception as e:
            warn(f"rm {p}: {e}")

    # Stop/disable/init cleanup for services once all files are handled
    if manifest_entries:
        remove_service_files(name, root, manifest_entries)
 

    conn.execute("DELETE FROM installed WHERE name=?", (name,))
    conn.execute(
        "INSERT INTO history(ts,action,name,from_ver,to_ver,details) VALUES(?,?,?,?,?,?)",
        (int(time.time()), "remove", name, meta["version"], None, None),
    )


def do_remove(names: List[str], root: Path, dry: bool, force: bool = False):
    global PROTECTED
    PROTECTED = load_protected()

    def worker(n: str):
        removepkg(name=n, root=root, dry_run=dry, force=force)

    max_workers = min(8, len(names))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_map = {ex.submit(worker, n): n for n in names}
        for fut in progress_bar(
            as_completed(future_map),
            total=len(future_map),
            desc="Removing",
            unit="pkg",
            colour="purple",
        ):
            try:
                fut.result()
            except Exception as e:
                warn(f"remove {future_map[fut]}: {e}")


def do_upgrade(targets: List[str], root: Path, dry: bool, verify: bool, force: bool = False):
    u = build_universe()
    goals = []
    if not targets:
        for n, meta in u.installed.items():
            goals.append(f"{n} ~= {meta['version']}")
    else:
        goals += targets

    try:
        plan = solve(goals, u)
    except ResolutionError:
        warn("SAT solver failed to find upgrade set, falling back to GitLab fetch...")
        for dep in targets:
            built = build_from_gitlab(dep)
            meta = installpkg(built, root=root, dry_run=dry, verify=verify, force=force, explicit=True)
            if not dry and meta and getattr(meta, "kernel", False):
                run_hook(
                    "kernel_install",
                    {
                        "LPM_PKG": meta.name,
                        "LPM_VERSION": meta.version,
                        "LPM_PRESET": meta.mkinitcpio_preset or "",
                    },
                )
        return

    upgrades = []
    for p in plan:
        cur = u.installed.get(p.name)
        if not cur or cmp_semver(p.version, cur["version"]) > 0:
            upgrades.append(p)

    if not upgrades:
        ok("Nothing to do.")
        return

    # Cleanup services before upgrade
    if upgrades:
        def svc_worker(p: PkgMeta):
            cur = u.installed.get(p.name)
            if cur:
                remove_service_files(p.name, Path(DEFAULT_ROOT), cur.get("manifest"))

        max_workers = min(8, len(upgrades))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(ex.map(svc_worker, upgrades))

    explicit_names = {n for n, m in u.installed.items() if m.get("explicit")}
    explicit_names |= set(targets)

    do_install(upgrades, root, dry, verify, force=force, explicit=explicit_names)


def autoremove(root: Path, dry: bool) -> None:
    conn = db()
    installed = db_installed(conn)
    conn.close()

    needed: Set[str] = {n for n, m in installed.items() if m.get("explicit")}
    changed = True
    while changed:
        changed = False
        for n in list(needed):
            meta = installed.get(n)
            if not meta:
                continue
            for req in meta.get("requires", []):
                req_name = req.split()[0]
                for mname, mmeta in installed.items():
                    if mname in needed:
                        continue
                    if req_name == mname or req_name in mmeta.get("provides", []):
                        if mname not in needed:
                            needed.add(mname)
                            changed = True

    to_remove = [n for n in installed.keys() if n not in needed]
    if not to_remove:
        ok("Nothing to autoremove.")
        return
    do_remove(sorted(to_remove), root, dry, force=False)


# =========================== Repo index generation =============================
def gen_index(repo_dir: Path, base_url: Optional[str], arch_filter: Optional[str] = None):
    """
    Generate index.json from all .zst packages in repo_dir.
    Only reads metadata (no extraction) using buffered mode.
    """
    repo_dir = repo_dir.resolve()
    packages = []

    for p in sorted(repo_dir.glob(f"*{EXT}")):
        try:
            with open_package_tar(p, stream=False) as tf:
                meta = None
                for m in tf.getmembers():
                    name = Path(m.name).name
                    if name == ".lpm-meta.json":
                        with tf.extractfile(m) as f:
                            meta = PkgMeta.from_dict(json.load(f))
                        break

                if not meta:
                    warn(f"{p.name}: missing .lpm-meta.json; skipping")
                    continue
                if arch_filter and not arch_compatible(meta.arch, arch_filter):
                    continue

                # Fill blob path and size
                meta.blob = (base_url.rstrip("/") + "/" + p.name) if base_url else ("file://" + str(p))
                try:
                    meta.size = p.stat().st_size
                except Exception:
                    pass

                packages.append(dataclasses.asdict(meta))

        except Exception as e:
            warn(f"{p.name}: {e}")

    index = {"generated": int(time.time()), "packages": packages}
    out = repo_dir / "index.json"
    write_json(out, index)
    ok(f"Wrote {out} with {len(packages)} packages")


# =========================== .lpmbuild support ================================
def _capture_lpmbuild_metadata(script: Path) -> Tuple[Dict[str,str], Dict[str,List[str]]]:
    """
    Source the .lpmbuild (bash) and dump scalars + arrays.
    """
    script_path = str(script.resolve())
    lines = [
        "set -e",
        f'source "{script_path}"',
        "_emit_scalar() {",
        '  n="$1"',
        '  if [[ ${!n+x} == x ]]; then',
        '    v="${!n}"',
        '    printf "__SCALAR__ %s=%s\\n" "$n" "$v"',
        '  fi',
        "}",
        "_emit_array() {",
        '  n="$1"',
        '  printf "__ARRAY__ %s\\n" "$n"',
        "  if declare -p \"$n\" 2>/dev/null | grep -q 'declare -a'; then",
        '    eval "for x in \\\"\\${${n}[@]}\\\"; do printf \\\"%s\\0\\\" \\\"\\$x\\\"; done"',
        '  elif [[ ${!n+x} == x ]]; then',
        '    v="${!n}"',
        '    if [[ -n "$v" ]]; then',
        '      printf "%s\\0" "$v"',
        '    fi',
        '  fi',
        '  printf "\\n"',
        "}",
        "for v in NAME VERSION RELEASE ARCH SUMMARY URL LICENSE CFLAGS KERNEL MKINITCPIO_PRESET install INSTALL; do _emit_scalar \"$v\"; done",
        "for a in SOURCE REQUIRES REQUIRES_PYTHON_DEPENDENCIES PROVIDES CONFLICTS OBSOLETES RECOMMENDS SUGGESTS; do _emit_array \"$a\"; done",
    ]
    bcmd = "\n".join(lines)

    try:
        proc = subprocess.run(["bash","-c", bcmd], capture_output=True, check=True)
    except subprocess.CalledProcessError as e:
        warn(f"lpmbuild parse failed: {e}")
        return {}, {}

    data = proc.stdout
    scalars: Dict[str,str] = {}
    arrays: Dict[str,List[str]] = {k: [] for k in [
        "SOURCE",
        "REQUIRES",
        "REQUIRES_PYTHON_DEPENDENCIES",
        "PROVIDES",
        "CONFLICTS",
        "OBSOLETES",
        "RECOMMENDS",
        "SUGGESTS",
    ]}

    i=0; n=len(data)
    while i < n:
        if data.startswith(b"__SCALAR__ ", i):
            j = data.find(b"\n", i)
            line = data[i+11:j].decode("utf-8", "replace")
            k,v = line.split("=",1)
            scalars[k]=v
            i = j+1
        elif data.startswith(b"__ARRAY__ ", i):
            j = data.find(b"\n", i)
            name = data[i+10:j].decode("utf-8","replace").strip()
            k = j+1
            t = data.find(b"\n", k)
            items = data[k:t].split(b"\0")
            items = [x.decode("utf-8","replace") for x in items if x]
            arrays.setdefault(name,[]).extend(items)
            i = t+1
        else:
            j = data.find(b"\n", i)
            if j==-1: break
            i = j+1

    install_value = None
    for key in ("INSTALL", "install"):
        value = scalars.get(key)
        if value:
            install_value = value
            break
    if install_value is not None:
        scalars["INSTALL"] = install_value

    return scalars, arrays

def _url_digest(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _source_cache_path(url: str, filename: str, *, digest: Optional[str] = None) -> Path:
    parsed = urllib.parse.urlparse(url)
    base = os.path.basename(parsed.path.rstrip("/")) or filename or "source"
    stem, ext = os.path.splitext(base)
    if not stem:
        stem = "source"
    if digest is None:
        digest = _url_digest(url)
    return SOURCE_CACHE_DIR / f"{stem}-{digest}{ext}"


def _cache_entry_filename(path: Path, *, digest: str) -> str:
    stem = path.stem
    if stem.endswith(f"-{digest}"):
        stem = stem[: -(len(digest) + 1)]
    return f"{stem}{path.suffix}"


def _maybe_fetch_source(url: str, dst_dir: Path, *, filename: Optional[str] = None):
    if not url:
        return
    parsed = urllib.parse.urlparse(url)
    fallback_fn = os.path.basename(parsed.path.rstrip("/"))

    cache_filename: Optional[str] = None
    cache_path: Optional[Path] = None
    url_digest = _url_digest(url)

    if filename:
        dst = dst_dir / filename
        if dst.exists():
            return
        candidate = _source_cache_path(url, filename, digest=url_digest)
        if candidate.exists():
            cache_path = candidate
            cache_filename = filename
    else:
        if fallback_fn:
            dst = dst_dir / fallback_fn
            if dst.exists():
                return
            candidate = _source_cache_path(url, fallback_fn, digest=url_digest)
            if candidate.exists():
                cache_path = candidate
                cache_filename = fallback_fn

    if cache_path is None:
        pattern = f"*-{url_digest}*"
        best_match: Optional[Path] = None
        for match in SOURCE_CACHE_DIR.glob(pattern):
            if best_match is None or match.name < best_match.name:
                best_match = match
        if best_match is not None:
            cache_path = best_match
            cache_filename = _cache_entry_filename(best_match, digest=url_digest)

    resolved_name = filename or cache_filename or fallback_fn
    if cache_path and cache_path.exists() and resolved_name:
        dst = dst_dir / resolved_name
        if dst.exists():
            return
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cache_path, dst)
            ok(f"Using cached source: {url}")
            return
        except Exception as e:
            warn(f"Failed to use cached source for {url}: {e}")

    ok(f"Fetching source: {url}")
    data, meta = urlread(url)

    resolved_name = filename
    if not resolved_name:
        inferred: Optional[str] = None
        if meta:
            if "://" in meta:
                final = urllib.parse.urlparse(meta)
                inferred = os.path.basename(final.path.rstrip("/"))
                query = urllib.parse.parse_qs(final.query)
                values = query.get("filename") or []
                if values:
                    inferred = values[-1]
                if inferred:
                    inferred = os.path.basename(inferred)
            else:
                inferred = os.path.basename(meta)
        if not inferred and cache_filename:
            inferred = cache_filename
        if not inferred and fallback_fn:
            inferred = fallback_fn
        resolved_name = inferred

    if not resolved_name:
        return

    dst = dst_dir / resolved_name
    if dst.exists():
        return

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)

    try:
        SOURCE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path = _source_cache_path(url, resolved_name, digest=url_digest)
        tmp_cache = cache_path.with_suffix(cache_path.suffix + ".tmp")
        tmp_cache.write_bytes(data)
        tmp_cache.replace(cache_path)
    except Exception as e:
        warn(f"Failed to cache source {url}: {e}")


def _fetch_git_source(url: str, dst_dir: Path, *, alias: Optional[str] = None) -> bool:
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme or ""
    if not scheme.startswith("git+"):
        return False

    actual_scheme = scheme.split("+", 1)[1] if "+" in scheme else "git"
    rewritten = parsed._replace(scheme=actual_scheme, fragment="", query="")
    git_url = urllib.parse.urlunparse(rewritten)

    target_name: Optional[str]
    if alias:
        target_name = alias
    else:
        target_name = os.path.basename(parsed.path.rstrip("/"))
        if target_name.endswith(".git"):
            target_name = target_name[: -len(".git")]
    if not target_name:
        target_name = "source"

    dst_dir.mkdir(parents=True, exist_ok=True)
    target_path = dst_dir / target_name
    if target_path.exists():
        return True

    tmp_target = target_path.parent / f".{target_path.name}.tmp"
    if tmp_target.exists():
        shutil.rmtree(tmp_target)

    ok(f"Fetching git source: {git_url}")

    clone_cmd = ["git", "clone", git_url, str(tmp_target)]
    subprocess.run(clone_cmd, check=True)

    fragment = urllib.parse.parse_qs(parsed.fragment, keep_blank_values=True)

    def _checkout(param: str):
        values = fragment.get(param)
        if not values:
            return
        ref = values[-1]
        if not ref:
            return
        subprocess.run(["git", "-C", str(tmp_target), "checkout", ref], check=True)

    _checkout("branch")
    _checkout("tag")
    _checkout("commit")

    tmp_target.replace(target_path)
    return True

# ======================= LPMBUILD =============================================
def fetch_lpmbuild(pkgname: str, dst: Path) -> Path:
    """
    Fetch a .lpmbuild script from the configured source repo.
    Default: https://gitlab.com/lpm-org/packages/-/raw/main/<pkg>/<pkg>.lpmbuild
    Can be overridden in /etc/lpm/lpm.conf:
      LPMBUILD_REPO=https://gitlab.com/myuser/myrepo/-/raw/main
    """
    base_url = CONF.get("LPMBUILD_REPO", "https://gitlab.com/lpm-org/packages/-/raw/main")

    url = f"{base_url.rstrip('/')}/{pkgname}/{pkgname}.lpmbuild"

    ok(f"Fetching lpmbuild for {pkgname} from {url}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        data, _ = urlread(url)
    except Exception as e:
        die(f"Failed to fetch lpmbuild for {pkgname}: {e}")
    dst.write_bytes(data)
    return dst


    
def build_from_gitlab(pkgname: str) -> Path:
    """
    Fetch lpmbuild from GitLab, build it if not already cached,
    and return the cached .zst package path.
    """
    cache_pkg = CACHE_DIR / f"{pkgname}.built{EXT}"
    if cache_pkg.exists():
        log(f"[cache] Using cached build for {pkgname}: {cache_pkg}")
        return cache_pkg

    tmp = Path(f"/tmp/lpm-dep-{pkgname}.lpmbuild")
    fetch_lpmbuild(pkgname, tmp)
    built, _, _, _ = run_lpmbuild(tmp, outdir=CACHE_DIR, prompt_install=False, is_dep=True, build_deps=True)

    # Copy to a stable cache filename
    if built != cache_pkg:
        try:
            shutil.copy2(built, cache_pkg)
        except Exception as e:
            warn(f"Failed to copy {built} to cache: {e}")
            return built
    return cache_pkg


def prompt_install_pkg(blob: Path, kind: str = "package", default: Optional[str] = None) -> None:
    """Prompt the user to install a built package.

    Parameters
    ----------
    blob : Path
        Path to the built package file.
    kind : str
        Human readable kind of object (package/dependency).
    default : Optional[str]
        Default answer if the user just presses Enter. If ``None`` the
        configuration value ``INSTALL_PROMPT_DEFAULT`` is used. Accepts
        ``"y"`` or ``"n"``.
    """
    try:
        meta, _ = read_package_meta(blob)
        desc = f"{meta.name}-{meta.version}-{meta.release}.{meta.arch}"
    except Exception:
        desc = blob.name

    if default is None:
        default = INSTALL_PROMPT_DEFAULT
    default = "y" if str(default).lower() in {"y", "yes"} else "n"
    choices = "[Y/n]" if default == "y" else "[y/N]"
    resp = input(f"{CYAN}[PROMPT]{RESET} Install {kind} {desc}? {choices} ").strip().lower()
    if not resp:
        resp = default
    if resp in {"y", "yes"}:
        installpkg(blob, explicit=(kind != "dependency"))


def run_lpmbuild(
    script: Path,
    outdir: Optional[Path] = None,
    *,
    prompt_install: bool = True,
    prompt_default: Optional[str] = None,
    is_dep: bool = False,
    build_deps: bool = True,
    fetcher: Optional[Callable[[str, Path], Path]] = None,
    _building_stack: Optional[Tuple[str, ...]] = None,
) -> Tuple[Path, float, int, List[Tuple[Path, PkgMeta]]]:
    script_path = script.resolve()
    script_dir = script_path.parent

    # --- Capture metadata first ---
    had_split_cmd = "LPM_SPLIT_PACKAGE" in os.environ
    if not had_split_cmd:
        os.environ["LPM_SPLIT_PACKAGE"] = shutil.which("true") or "/bin/true"

    try:
        scal, arr = _capture_lpmbuild_metadata(script_path)
    finally:
        if not had_split_cmd:
            with contextlib.suppress(KeyError):
                del os.environ["LPM_SPLIT_PACKAGE"]
    name = scal.get("NAME", "")
    version = scal.get("VERSION", "")
    release = scal.get("RELEASE", "1")
    arch = (scal.get("ARCH") or ARCH or "").strip()
    if not arch:
        arch = PkgMeta.__dataclass_fields__["arch"].default
    summary = scal.get("SUMMARY", "")
    url = scal.get("URL", "")
    license_ = scal.get("LICENSE", "")
    kernel = scal.get("KERNEL", "").lower() == "true"
    mkinitcpio_preset = scal.get("MKINITCPIO_PRESET") or None
    if not name or not version:
        die("lpmbuild missing NAME or VERSION")

    building_stack = tuple(_building_stack or ())
    if name in building_stack:
        cycle = " -> ".join((*building_stack, name))
        die(f"dependency cycle detected: {cycle}")
    building_stack = (*building_stack, name)
    building_stack_set = set(building_stack)

    # --- Auto-build dependencies before continuing ---
    fetch_fn = fetcher or fetch_lpmbuild

    if build_deps:
        seen: Set[Tuple[str, str]] = set()
        deps_to_build: List[str] = []
        python_to_build: List[Tuple[str, str]] = []

        capabilities: Set[str] = set()

        def _canonical_capability(value: str) -> Optional[str]:
            if not isinstance(value, str):
                return None
            capability = re.split(r"[<>=]", value, 1)[0].strip()
            if not capability:
                return None
            parts = capability.split()
            return parts[0] if parts else capability

        def _register_meta_capabilities(meta: PkgMeta) -> None:
            if not meta:
                return
            if meta.name:
                capabilities.add(meta.name)
            for provide in getattr(meta, "provides", []) or []:
                cap = _canonical_capability(provide)
                if cap:
                    capabilities.add(cap)

        conn = db()
        try:
            installed = db_installed(conn)
            capabilities.update(installed)
            for pkg_name, meta in installed.items():
                if pkg_name:
                    capabilities.add(pkg_name)
                provides = meta.get("provides") if isinstance(meta, dict) else getattr(meta, "provides", None)
                if not provides:
                    continue
                for provide in provides:
                    cap = _canonical_capability(provide)
                    if cap:
                        capabilities.add(cap)
        finally:
            conn.close()

        for dep in arr.get("REQUIRES", []):
            try:
                e = parse_dep_expr(dep)
            except Exception:
                continue
            parts = flatten_and(e) if e.kind == "and" else [e]
            for part in parts:
                if part.kind == "atom":
                    depname = part.atom.name
                    key = ("pkg", depname)
                    if key in seen:
                        continue
                    seen.add(key)

                    if depname not in capabilities:
                        deps_to_build.append(depname)

        def _register_built_dependency_blob(blob: Path) -> None:
            if not blob:
                return
            try:
                meta, _ = read_package_meta(blob)
            except Exception as exc:
                warn(f"[deps] unable to inspect built dependency {blob}: {exc}")
                return
            _register_meta_capabilities(meta)

        def _build_dep(depname: str, idx: Optional[int] = None, total: Optional[int] = None):
            if idx is not None and total is not None:
                log(f"[deps] ({idx}/{total}) building required package: {depname}")
            else:
                log(f"[deps] building required package: {depname}")
            if depname in building_stack_set:
                cycle = " -> ".join((*building_stack, depname))
                die(f"dependency cycle detected: {cycle}")
            tmp = Path(f"/tmp/lpm-dep-{depname}.lpmbuild")
            fetch_fn(depname, tmp)
            return run_lpmbuild(
                tmp,
                outdir or script_dir,
                prompt_install=prompt_install,
                prompt_default=prompt_default,
                is_dep=True,
                build_deps=True,
                fetcher=fetch_fn,
                _building_stack=building_stack,
            )[0]

        if deps_to_build:
            if prompt_install:
                total = len(deps_to_build)
                with progress_bar(
                    deps_to_build,
                    unit="pkg",
                    mode="ninja",
                    leave=True,
                ) as pbar:
                    for idx, dep in enumerate(pbar, start=1):
                        pbar.set_description(f"[deps] {dep}")
                        blob = _build_dep(dep, idx, total)
                        _register_built_dependency_blob(blob)
            else:
                max_workers = min(4, len(deps_to_build))
                with ThreadPoolExecutor(max_workers=max_workers) as ex:
                    for blob in progress_bar(
                        ex.map(_build_dep, deps_to_build),
                        total=len(deps_to_build),
                        desc="[deps] building",
                        unit="pkg",
                        mode="ninja",
                        leave=True,
                    ):
                        _register_built_dependency_blob(blob)

        for raw_spec in arr.get("REQUIRES_PYTHON_DEPENDENCIES", []):
            spec = (raw_spec or "").strip()
            if not spec:
                continue
            try:
                requirement = Requirement(spec)
            except Exception as exc:
                warn(f"[deps] invalid Python dependency '{spec}': {exc}")
                continue
            canonical_name = canonicalize_name(requirement.name or "")
            if not canonical_name:
                continue
            capability_names = {canonical_name}
            trimmed_name: Optional[str] = None
            if canonical_name.startswith("python-"):
                trimmed_name = canonical_name[len("python-") :]
                if trimmed_name:
                    capability_names.add(trimmed_name)
            if any(f"pypi({name})" in capabilities for name in capability_names):
                continue
            key = ("python", canonical_name)
            if key in seen:
                continue
            alt_keys: List[Tuple[str, str]] = []
            if trimmed_name:
                alt_key = ("python", trimmed_name)
                if alt_key in seen:
                    continue
                alt_keys.append(alt_key)
            seen.add(key)
            for alt_key in alt_keys:
                seen.add(alt_key)
            python_to_build.append((spec, canonical_name))

        def _build_python_dep(spec: str, canonical_name: str, idx: Optional[int] = None, total: Optional[int] = None):
            if idx is not None and total is not None:
                log(f"[deps] ({idx}/{total}) building required Python package: {spec}")
            else:
                log(f"[deps] building required Python package: {spec}")
            out, meta, _ = build_python_package_from_pip(
                spec,
                outdir or script_dir,
                include_deps=True,
            )
            _register_meta_capabilities(meta)
            provided_caps = [
                cap
                for cap in getattr(meta, "provides", []) or []
                if isinstance(cap, str) and cap.startswith("pypi(") and cap.endswith(")")
            ]
            if provided_caps:
                capabilities.update(provided_caps)
            else:
                meta_canonical = canonicalize_name(getattr(meta, "name", "") or "")
                if meta_canonical:
                    capabilities.add(f"pypi({meta_canonical})")
            if prompt_install:
                prompt_install_pkg(out, kind="dependency", default=prompt_default)
            return out

        if python_to_build:
            total = len(python_to_build)
            with progress_bar(
                python_to_build,
                unit="pkg",
                mode="ninja",
                leave=True,
            ) as pbar:
                for idx, (spec, canonical_name) in enumerate(pbar, start=1):
                    pbar.set_description(f"[deps] pip {canonical_name}")
                    _build_python_dep(spec, canonical_name, idx, total)

    stagedir = Path(f"/tmp/pkg-{name}")
    buildroot = Path(f"/tmp/build-{name}")
    srcroot   = Path(f"/tmp/src-{name}")

    split_meta = {
        "name": name,
        "version": version,
        "release": release,
        "arch": arch,
        "summary": summary,
        "url": url,
        "license": license_,
        "requires": arr.get("REQUIRES", []),
        "provides": arr.get("PROVIDES", []),
        "conflicts": arr.get("CONFLICTS", []),
        "obsoletes": arr.get("OBSOLETES", []),
        "recommends": arr.get("RECOMMENDS", []),
        "suggests": arr.get("SUGGESTS", []),
        "kernel": kernel,
        "mkinitcpio_preset": mkinitcpio_preset,
    }
    tmp_files: List[Path] = []

    with tempfile.NamedTemporaryFile(
        prefix="lpm-split-meta-",
        suffix=".json",
        delete=False,
        mode="w",
        encoding="utf-8",
    ) as f:
        json.dump(split_meta, f)
        split_meta_path = Path(f.name)
        tmp_files.append(split_meta_path)

    with tempfile.NamedTemporaryFile(
        prefix="lpm-split-record-",
        suffix=".jsonl",
        delete=False,
        mode="w",
        encoding="utf-8",
    ) as f:
        split_record_path = Path(f.name)
        tmp_files.append(split_record_path)

    for d in (stagedir, buildroot, srcroot):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    helper_name = "lpm-split-package"
    helper_path = buildroot / helper_name
    exec_candidate = getattr(sys, "executable", None)
    exec_path: Optional[Path] = None
    if exec_candidate:
        try:
            exec_path = Path(exec_candidate)
        except TypeError:
            exec_path = None

    module_path = Path(__file__).resolve()
    is_frozen = bool(getattr(sys, "frozen", False))
    use_argv0 = is_frozen or exec_path is None or not exec_path.exists()

    if use_argv0:
        argv0 = sys.argv[0] if sys.argv else None
        if argv0:
            command_path = Path(argv0).resolve()
        elif exec_path is not None:
            command_path = exec_path.resolve()
        else:
            command_path = module_path
        helper_cmd = [shlex.quote(str(command_path)), "splitpkg"]
    else:
        command_path = exec_path.resolve()
        helper_cmd = [
            shlex.quote(str(command_path)),
            shlex.quote(str(module_path)),
            "splitpkg",
        ]

    helper_path.write_text(
        "#!/bin/sh\n"
        f"exec {' '.join(helper_cmd)} \"$@\"\n",
        encoding="utf-8",
    )
    helper_path.chmod(0o755)
    helper_env_path: Path
    sandbox_mode = CONF.get("SANDBOX_MODE", "none").lower()
    if sandbox_mode == "bwrap":
        helper_env_path = Path("/build") / helper_name
    else:
        helper_env_path = helper_path

    env = os.environ.copy()
    env.update({
        "DESTDIR": str(stagedir),
        "pkgdir": str(stagedir),
        "BUILDROOT": str(buildroot),
        "SRCROOT": str(srcroot),
        "LPM_SPLIT_PACKAGE": str(helper_env_path),
        "LPM_SPLIT_BASE_META": str(split_meta_path),
        "LPM_SPLIT_RECORD": str(split_record_path),
        "LPM_SPLIT_OUTDIR": str(outdir or script_dir),
    })

    base_flags = f"{OPT_LEVEL} -march={MARCH} -mtune={MTUNE} -pipe -fPIC"
    extra_cflags = " ".join(filter(None, [env.get("CFLAGS", "").strip(), scal.get("CFLAGS", "").strip()]))
    flags = f"{base_flags} {extra_cflags}".strip()
    env["CFLAGS"] = flags
    env["CXXFLAGS"] = flags
    env["LDFLAGS"] = OPT_LEVEL
    log(f"[opt] vendor={CPU_VENDOR} family={CPU_FAMILY} -> {flags}")

    sources = []
    for raw_entry in arr.get("SOURCE", []):
        entry = raw_entry.strip()
        if entry:
            sources.append(entry)

    fetch_url_opt_in = scal.get("FETCH_URL", "").strip().lower() in {"1", "true", "yes", "on"}
    if fetch_url_opt_in or not sources:
        # Auto-fetch source if URL provided and explicitly requested or no SOURCE entries exist
        _maybe_fetch_source(url, srcroot)

    base_repo = CONF.get("LPMBUILD_REPO", "https://gitlab.com/lpm-org/packages/-/raw/main").rstrip("/")
    base_source_prefix = f"{base_repo}/{name}/"
    for entry in sources:
        alias: Optional[str] = None
        source_ref = entry
        if "::" in entry:
            alias, source_ref = entry.split("::", 1)
            alias = alias.strip() or None
            source_ref = source_ref.strip()

        parsed = urllib.parse.urlparse(source_ref)

        if not parsed.scheme and not os.path.isabs(source_ref):
            local_candidate = (script_dir / source_ref).resolve()
            if local_candidate.exists():
                target_name = alias or os.path.basename(source_ref.rstrip("/"))
                if target_name:
                    target_path = srcroot / target_name
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(local_candidate, target_path)
                continue

            source_ref = urllib.parse.urljoin(f"{base_source_prefix}", source_ref)
            parsed = urllib.parse.urlparse(source_ref)

        if not parsed.scheme:
            local_candidate = (script_dir / source_ref).resolve()
            if local_candidate.exists():
                target_name = alias or os.path.basename(source_ref.rstrip("/"))
                if target_name:
                    target_path = srcroot / target_name
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(local_candidate, target_path)
                continue

        if _fetch_git_source(source_ref, srcroot, alias=alias):
            continue

        _maybe_fetch_source(source_ref, srcroot, filename=alias)

    staged_entries: List[str] = []
    for path in sorted(srcroot.rglob("*")):
        if path.is_file() or path.is_symlink():
            try:
                rel = path.relative_to(srcroot)
            except ValueError:
                rel = path
            staged_entries.append(str(rel))

    def _iter_stage_candidates(entries: Iterable[str]) -> Iterable[Path]:
        for raw in entries:
            entry = raw.strip()
            if not entry:
                continue
            candidate = Path(entry)
            if not candidate.is_absolute():
                candidate = srcroot / candidate
            yield candidate

    signature_suffixes = (".sig", ".asc")
    extra_compressed_exts = (".tar.zst", ".tar.lz4", ".tar.lzo")

    def _is_signature(path: Path) -> bool:
        name = path.name.lower()
        return any(name.endswith(suffix) for suffix in signature_suffixes)

    def _is_extractable(path: Path) -> bool:
        if not path.is_file():
            return False
        if _is_signature(path):
            return False
        if tarfile.is_tarfile(path):
            return True
        lowered = path.name.lower()
        return any(lowered.endswith(ext) for ext in extra_compressed_exts)

    archive_path: Optional[Path] = None
    for candidate in _iter_stage_candidates(staged_entries):
        if _is_extractable(candidate):
            archive_path = candidate
            break
    if archive_path is None:
        for candidate in sorted(srcroot.rglob("*")):
            if _is_extractable(candidate):
                archive_path = candidate
                break

    if archive_path is not None:
        target_dir = srcroot / f"{name}-{version}"
        target_dir.mkdir(parents=True, exist_ok=True)

        def _extract_with_tarfile(path: Path, dest: Path) -> None:
            def _strip_member(name: str) -> Optional[str]:
                parts = [part for part in name.split("/") if part and part != "."]
                if len(parts) <= 1:
                    return None
                return "/".join(parts[1:])

            fileobj: Optional[io.BytesIO] = None
            tf: Optional[tarfile.TarFile] = None
            try:
                lowered_name = path.name.lower()
                if lowered_name.endswith(".tar.zst"):
                    data = zstd.ZstdDecompressor().decompress(path.read_bytes())
                    fileobj = io.BytesIO(data)
                    tf = tarfile.open(fileobj=fileobj, mode="r:")
                else:
                    tf = tarfile.open(path)

                members = []
                for member in tf.getmembers():
                    stripped = _strip_member(member.name)
                    if stripped is None:
                        continue
                    member.name = stripped
                    members.append(member)
                tf.extractall(dest, members=members)
            finally:
                if tf is not None:
                    tf.close()
                if fileobj is not None:
                    fileobj.close()

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
            with contextlib.suppress(Exception):
                _extract_with_tarfile(archive_path, target_dir)

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
        sandboxed_run(
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
                script_text = generate_install_script(stagedir)
                with install_sh.open("w", encoding="utf-8") as f:
                    f.write("#!/bin/sh\nset -e\n")
                    f.write(script_text)
                    if not script_text.endswith("\n"):
                        f.write("\n")
                install_sh.chmod(0o755)
        except Exception as e:
            warn(f"Could not embed install script for {name}: {e}")

    # --- Package metadata ---
    meta = PkgMeta(
        name=name, version=version, release=release, arch=arch,
        summary=summary, url=url, license=license_,
        requires=arr.get("REQUIRES", []),
        provides=arr.get("PROVIDES", []),
        conflicts=arr.get("CONFLICTS", []),
        obsoletes=arr.get("OBSOLETES", []),
        recommends=arr.get("RECOMMENDS", []),
        suggests=arr.get("SUGGESTS", []),
        kernel=kernel,
        mkinitcpio_preset=mkinitcpio_preset,
    )

    outdir = script_dir if outdir is None else outdir
    out = outdir / f"{meta.name}-{meta.version}-{meta.release}.{meta.arch}{EXT}"
    build_package(stagedir, meta, out, sign=True)
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

    if prompt_install:
        prompt_install_pkg(out, kind="dependency" if is_dep else "package", default=prompt_default)
        for split_path, _split_meta in split_records:
            prompt_install_pkg(split_path, kind="split package", default=prompt_default)
    return out, duration, phase_count, split_records

# =========================== CLI commands =====================================
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
        print(f"Conflicts:  {', '.join(p.conflicts) or '-'}")
        print(f"Obsoletes:  {', '.join(p.obsoletes) or '-'}")
        print(f"Recommends: {', '.join(p.recommends) or '-'}")
        print(f"Suggests:   {', '.join(p.suggests) or '-'}")
        print(f"Blob:       {p.blob or '-'}")

def cmd_install(a):
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


def cmd_bootstrap(a):
    root = Path(a.root)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        die(f"could not create root {root}: {e}")

    for d in ["dev", "proc", "sys", "tmp", "var", "etc"]:
        try:
            (root / d).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            die(f"could not create {d}: {e}")

    base_pkgs = ["lpm-base", "lpm-core"]
    pkgs = base_pkgs + list(a.include or [])
    try:
        plan = solve(pkgs, build_universe())
    except ResolutionError as e:
        die(f"dependency resolution failed: {e}")

    log("[plan] bootstrap install order:")
    for p in plan:
        log(f"  - {p.name}-{p.version}")

    try:
        do_install(plan, root, dry=False, verify=(not a.no_verify), force=False, explicit=set(pkgs))
    except SystemExit:
        raise
    except Exception as e:
        die(f"install failed: {e}")

    try:
        shutil.copy2("/etc/resolv.conf", root / "etc/resolv.conf")
    except Exception as e:
        warn(f"could not copy resolv.conf: {e}")


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
            cur = u.installed.get(p.name)
            if cur:
                remove_service_files(p.name, Path(DEFAULT_ROOT), cur.get("manifest"))
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
        write_json(PIN_FILE, pins); ok("Updated holds")
    elif a.action=="unhold":
        pins.setdefault("hold",[])
        pins["hold"]=[n for n in pins["hold"] if n not in a.names]
        write_json(PIN_FILE, pins); ok("Updated holds")
    elif a.action=="prefer":
        pins.setdefault("prefer",{})
        for s in a.prefs:
            if ":" not in s: die("use name:constraint, e.g. openssl:~=3.3")
            name,cons = s.split(":",1)
            pins["prefer"][name]=cons
        write_json(PIN_FILE, pins); ok("Updated preferences")

def cmd_build(a):
    stagedir=Path(a.stagedir)
    meta = PkgMeta(
        name=a.name, version=a.version, release=a.release, arch=a.arch,
        summary=a.summary, url=a.url, license=a.license,
        requires=a.requires, provides=a.provides, conflicts=a.conflicts,
        obsoletes=a.obsoletes, recommends=a.recommends, suggests=a.suggests
    )
    out = Path(a.output or f"{meta.name}-{meta.version}-{meta.release}.{meta.arch}{EXT}")
    build_package(stagedir, meta, out, sign=(not a.no_sign))
    prompt_install_pkg(out, default=a.install_default)

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

    def _merge_list(opt_name: str) -> List[str]:
        opt = getattr(a, opt_name, None)
        if opt:
            return [str(x) for x in opt]
        base = base_meta.get(opt_name)
        if isinstance(base, list):
            return [str(x) for x in base]
        return []

    requires = _merge_list("requires")
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
        requires=requires,
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
    out.parent.mkdir(parents=True, exist_ok=True)

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
                script_text = generate_install_script(stagedir)
                with install_sh.open("w", encoding="utf-8") as f:
                    f.write("#!/bin/sh\nset -e\n")
                    f.write(script_text)
                    if not script_text.endswith("\n"):
                        f.write("\n")
                install_sh.chmod(0o755)
            except Exception as exc:
                warn(f"Could not embed install script for {name}: {exc}")

    build_package(stagedir, meta, out, sign=(not a.no_sign))

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
    if a.python_pip:
        if a.script:
            die("Cannot specify both a .lpmbuild script and --python-pip")
        out, meta, duration = build_python_package_from_pip(
            a.python_pip,
            a.outdir,
            include_deps=not a.no_deps,
        )
        prompt_install_pkg(out, default=a.install_default)
        print_build_summary(meta, out, duration, len(meta.requires), 1)
        ok(f"Built {out}")
        return

    if not a.script:
        die("buildpkg requires a .lpmbuild script or --python-pip")

    script_path = Path(a.script)
    if not script_path.exists():
        die(f".lpmbuild script not found: {script_path}")

    out, duration, phases, splits = run_lpmbuild(
        script_path,
        a.outdir,
        build_deps=not a.no_deps,
        prompt_default=a.install_default,
    )

    if out and out.exists():
        meta, _ = read_package_meta(out)
        print_build_summary(meta, out, duration, len(meta.requires), phases)
        if splits:
            for spath, smeta in splits:
                ok(f"Split: {spath} ({smeta.name})")
        ok(f"Built {out}")
    else:
        die(f"Build failed for {a.script}")


def cmd_genindex(a):
    repo_dir = Path(a.repo_dir)
    gen_index(repo_dir, a.base_url, arch_filter=a.arch)

def cmd_clean_cache(_):
    if CACHE_DIR.exists():
        for p in CACHE_DIR.iterdir():
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

def installpkg(
    file: Path,
    root: Path = Path(DEFAULT_ROOT),
    dry_run: bool = False,
    verify: bool = True,
    force: bool = False,
    explicit: bool = False,
    allow_fallback: bool = ALLOW_LPMBUILD_FALLBACK,
    hook_transaction: Optional[HookTransactionManager] = None,
    register_event: bool = True,
):
    """
    Production-grade .zst package installer with protected package + dep resolution.
    """
    global PROTECTED
    PROTECTED = load_protected()

    txn = hook_transaction
    owns_txn = False
    if txn is None and not dry_run:
        txn = HookTransactionManager(
            hooks=load_hooks(LIBLPM_HOOK_DIRS),
            root=root,
            base_env={"LPM_ROOT": str(root)},
        )
        owns_txn = True

    # --- Step 1: Validate extension + magic ---
    if file.suffix != EXT:
        die(f"{file.name} is not a {EXT} package")
    try:
        with file.open("rb") as f:
            magic = f.read(4)
        if magic != b"\x28\xb5\x2f\xfd":
            die(f"{file.name} is not a valid {EXT} (bad magic header)")
    except Exception as e:
        die(f"Cannot read {file}: {e}")

    # --- Step 2: Signature verification ---
    sig = file.with_suffix(file.suffix + ".sig")
    if verify:
        if not sig.exists():
            die(f"Missing signature: {sig}")
        verify_signature(file, sig)

        # --- Step 3: Read metadata ---
    meta, mani = read_package_meta(file)
    if not meta:
        die(f"Invalid package: {file.name} (no metadata)")
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

        run_hook("pre_install", dict(hook_env))

        tmp_root = Path(tempfile.mkdtemp(prefix=f"lpm-{meta.name}-", dir="/tmp"))
        try:
            manifest = extract_tar(file, tmp_root)

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

            # Move into root w/ conflict handling (same as before) ...
            replace_all = False
            for e in mani:
                rel = e["path"].lstrip("/")
                src = tmp_root / rel
                dest = root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)

                if src.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue

                if dest.exists() or dest.is_symlink():
                    same = False
                    try:
                        if dest.is_file() and sha256sum(dest) == e["sha256"]:
                            same = True
                    except Exception:
                        pass
                    if same:
                        log(f"[skip] {rel} already up-to-date")
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
                                f"[conflict] {rel} exists. [R]eplace / [RA] Replace All / [S]kip / [A]bort? "
                            ).strip().lower()
                            if resp in ("r", "replace"):
                                _remove_dest()
                                break
                            elif resp in ("ra", "all", "replace all"):
                                replace_all = True
                                _remove_dest()
                                break
                            elif resp in ("s", "skip"):
                                log(f"[skip] {rel}")
                                src.unlink(missing_ok=True)
                                continue
                            elif resp in ("a", "abort"):
                                die(f"Aborted install due to conflict at {rel}")
                            else:
                                print("Please enter R, RA, S, or A.")

                shutil.move(str(src), str(dest))

            install_script_rel = "/.lpm-install.sh"
            staged_script = tmp_root / install_script_rel.lstrip("/")
            installed_script = root / install_script_rel.lstrip("/")
            script_entry = next((e for e in mani if e["path"] == install_script_rel), None)

            script_to_run = None
            if installed_script.exists():
                script_to_run = installed_script
            elif staged_script.exists():
                script_to_run = staged_script

            if script_to_run and os.access(script_to_run, os.X_OK):
                env = os.environ.copy()
                env.update({
                    "LPM_ROOT": str(root),
                    "LPM_PKG": meta.name,
                    "LPM_VERSION": meta.version,
                    "LPM_RELEASE": meta.release,
                })

                action = "upgrade" if previous_version is not None else "install"
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

                log(f"[lpm] Running embedded install script: {script_to_run}")
                subprocess.run(argv, check=False, cwd=str(root), env=env)

            if script_entry and not script_entry.get("keep", False):
                for candidate in (installed_script, staged_script):
                    try:
                        candidate.unlink()
                    except FileNotFoundError:
                        pass
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

        run_hook("post_install", dict(hook_env))

        if previous_version is not None:
            run_hook("post_upgrade", dict(hook_env))
        
        # New: init system service integration
        handle_service_files(meta.name, root, mani)

    if txn is not None and owns_txn:
        txn.run_post_transaction()

    ok(f"Installed {meta.name}-{meta.version}-{meta.release}.{meta.arch}")
    return meta


def removepkg(
    name: str,
    root: Path = Path(DEFAULT_ROOT),
    dry_run: bool = False,
    force: bool = False,
    hook_transaction: Optional[HookTransactionManager] = None,
    register_event: bool = True,
):
    global PROTECTED
    PROTECTED = load_protected()

    root = Path(root)
    txn = hook_transaction
    owns_txn = False
    if txn is None and not dry_run:
        txn = HookTransactionManager(
            hooks=load_hooks(LIBLPM_HOOK_DIRS),
            root=root,
            base_env={"LPM_ROOT": str(root)},
        )
        owns_txn = True

    if name in PROTECTED and not force:
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

    if txn is not None and not dry_run:
        txn.ensure_pre_transaction()

    with transaction(conn, f"remove {name}", dry_run):
        run_hook("pre_remove", {"LPM_PKG": name, "LPM_ROOT": str(root)})
        _remove_installed_package(meta, root, dry_run, conn)
        run_hook("post_remove", {"LPM_PKG": name, "LPM_ROOT": str(root)})

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
            write_json(PROTECTED_FILE, {"protected": sorted(current)})
            ok("Updated protected list")
        else:
            log("No changes")
    elif a.action == "remove":
        new = [n for n in current if n not in a.names]
        if new != current:
            write_json(PROTECTED_FILE, {"protected": sorted(new)})
            ok("Updated protected list")
        else:
            log("No changes")


def cmd_setup(_):
    run_first_run_wizard()


# =========================== Argparse / main ==================================
def build_parser()->argparse.ArgumentParser:
    p=argparse.ArgumentParser(prog="lpm", description="Linux Package Manager with SAT solver, signatures, and .lpmbuild")
    sub=p.add_subparsers(dest="cmd", required=True)

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

    sp=sub.add_parser("upgrade", help="Upgrade packages (targets or all)")
    sp.add_argument("names", nargs="*")
    sp.add_argument("--root")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--no-verify", action="store_true", help="skip signature verification (DANGEROUS)")
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
    sp.add_argument("--outdir", default=Path.cwd(), type=Path)
    sp.add_argument("--no-deps", action="store_true", help="do not fetch or build dependencies")
    sp.add_argument("--install-default", choices=["y", "n"], help="default answer for install prompt")
    sp.add_argument("--python-pip", metavar="SPEC", help="build a package from a Python distribution fetched via pip")
    sp.set_defaults(func=cmd_buildpkg)

    sp=sub.add_parser("genindex", help=f"Generate index.json for a repo directory of {EXT} files")
    sp.add_argument("repo_dir", help=f"directory containing {EXT} files")
    sp.add_argument("--base-url", dest="base_url", help="base URL for blobs in index (e.g., https://repo.example.com)", default=None)
    sp.add_argument("--arch", help="only include this arch (noarch always included)", default=None)
    sp.set_defaults(func=cmd_genindex)
    
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

    sp=sub.add_parser("bootstrap", help="Create a base chroot system")
    sp.add_argument("root", help="target directory for the new system")
    sp.add_argument("--include", nargs="*", default=[], help="extra packages to add")
    sp.add_argument("--no-verify", action="store_true", help="skip signature verification")
    sp.set_defaults(func=cmd_bootstrap)

    sp = sub.add_parser("protected", help="Show or edit protected package list")
    sp.add_argument("action", choices=["list", "add", "remove"])
    sp.add_argument("names", nargs="*", help="package names (for add/remove)")
    sp.set_defaults(func=cmd_protected)


    return p

def main(argv=None):
    args=build_parser().parse_args(argv)
    if getattr(args, "cmd", None) != "setup" and not CONF_FILE.exists():
        run_first_run_wizard()
    try:
        args.func(args)
    except ResolutionError as e:
        die(f"dependency resolution failed: {e}")

if __name__=="__main__":
    main()

