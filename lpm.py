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
import argparse, contextlib, dataclasses, fnmatch, hashlib, io, json, os, re, shlex, shutil, sqlite3, subprocess, sys, tarfile, tempfile, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Iterable
import zstandard as zstd

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
os.umask(UMASK)
for d in (STATE_DIR, CACHE_DIR, SNAPSHOT_DIR): d.mkdir(parents=True, exist_ok=True)
if not REPO_LIST.exists(): REPO_LIST.write_text("[]", encoding="utf-8")
if not PIN_FILE.exists(): PIN_FILE.write_text(json.dumps({"hold":[],"prefer":{}}, indent=2), encoding="utf-8")

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

# Progress bar wrapper
from tqdm import tqdm

def progress_bar(iterable, desc="Processing", unit="item"):
    return tqdm(iterable, desc=desc, unit=unit, ncols=80, colour="cyan")

# =========================== JSON / Config ====================================
def read_json(p: Path):
    with p.open("r", encoding="utf-8") as f: return json.load(f)
def write_json(p: Path, obj):
    tmp = p.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f: json.dump(obj, f, indent=2, sort_keys=True)
    tmp.replace(p)

def urlread(url: str) -> bytes:
    with urllib.request.urlopen(url) as r:
        total = int(r.headers.get("content-length", 0) or 0)
        if total == 0:
            return r.read()
        chunk_size = 1 << 14
        data = bytearray()
        with tqdm(total=total, desc="Downloading", unit="B", unit_scale=True, ncols=80, colour="cyan") as bar:
            while True:
                chunk = r.read(chunk_size)
                if not chunk: break
                data.extend(chunk)
                bar.update(len(chunk))
        return bytes(data)

def load_conf(path: Path) -> Dict[str,str]:
    if not path.exists(): return {}
    out = {}
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"): continue
        if "=" in ln:
            k, v = ln.split("=",1)
            out[k.strip()] = v.strip()
    return out
    
# ================ CONFIG LOADER:: /etc/lpm/lpm.conf ====
CONF = load_conf(CONF_FILE)
ARCH = CONF.get("ARCH", os.uname().machine if hasattr(os, "uname") else "x86_64")

# --- Optimization level (-O2 etc.) ---
OPT_LEVEL = CONF.get("OPT_LEVEL", "-O2")
if OPT_LEVEL not in ("-Os", "-O2", "-O3", "-Ofast"):
    OPT_LEVEL = "-O2"


def _detect_cpu() -> Tuple[str, str, str, str]:
    """Return (march, mtune, vendor, family)."""
    vendor = family = ""  # defaults
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            for line in f:
                if not vendor and line.startswith("vendor_id"):
                    vendor = line.split(":", 1)[1].strip()
                elif not family and line.startswith("cpu family"):
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

    return march, mtune, vendor, family


MARCH, MTUNE, CPU_VENDOR, CPU_FAMILY = _detect_cpu()

# ================ Init System Detection ===============================================
def detect_init_system() -> str:
    """
    Detect which init system is active.
    Returns: 'systemd', 'runit', 'openrc', 'sysv', or 'unknown'
    """
    if shutil.which("systemctl") and os.path.isdir("/run/systemd/system"):
        return "systemd"
    if os.path.isdir("/etc/runit") or os.path.isdir("/etc/runit/runsvdir"):
        return "runit"
    if os.path.isdir("/etc/init.d"):
        if shutil.which("openrc"):
            return "openrc"
        return "sysv"
    return "unknown"
    
    
# ============================ Build Isolation =======================
def sandboxed_run(func: str, cwd: Path, env: dict, script_path: Path, stagedir: Path, buildroot: Path, srcroot: Path):
    """
    Run build function inside sandbox depending on SANDBOX_MODE.
    Supports: none, fakeroot, bwrap.
    """
    mode = CONF.get("SANDBOX_MODE", "none").lower()

    if mode == "fakeroot":
        cmd = ["fakeroot", "bash", "-c", f'set -e; source "{script_path}"; {func}']
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
            "bash", "-c", f'set -e; cd /src; source "{script_path.name}"; {func}'
        ]
        subprocess.run(cmd, check=True, env=env, cwd=str(cwd))
        return

    # Default: no sandbox
    cmd = ["bash", "-c", f'set -e; source "{script_path}"; {func}']
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
    recommends: List[str] = field(default_factory=list)
    suggests: List[str] = field(default_factory=list)
    size: int = 0
    sha256: Optional[str] = None
    blob: Optional[str] = None
    repo: str = ""
    prio: int = 10
    @staticmethod
    def from_dict(d: dict, repo_name="(local)", prio=0) -> "PkgMeta":
        return PkgMeta(
            name=d["name"], version=d["version"], release=d.get("release","1"),
            arch=d.get("arch","noarch"), summary=d.get("summary",""), url=d.get("url",""),
            license=d.get("license",""), requires=d.get("requires",[]), conflicts=d.get("conflicts",[]),
            obsoletes=d.get("obsoletes",[]), provides=d.get("provides",[]), recommends=d.get("recommends",[]),
            suggests=d.get("suggests",[]), size=d.get("size",0), sha256=d.get("sha256"), blob=d.get("blob"),
            repo=repo_name, prio=prio)

# =========================== Repos ============================================
@dataclass
class Repo: 
    name: str
    url: str
    priority: int=10

def list_repos() -> List[Repo]: 
    return [Repo(**r) for r in read_json(REPO_LIST)]

def save_repos(rs: List[Repo]): 
    write_json(REPO_LIST, [dataclasses.asdict(r) for r in rs])

def add_repo(name,url,priority=10):
    rs=list_repos()
    if any(r.name==name for r in rs): die(f"repo {name} exists")
    rs.append(Repo(name,url,priority)); save_repos(rs); ok(f"Added repo {name}")

def del_repo(name):
    save_repos([r for r in list_repos() if r.name!=name]); ok(f"Removed repo {name}")

def fetch_repo_index(repo: Repo) -> List[PkgMeta]:
    idx_url = repo.url.rstrip("/") + "/index.json"
    j = json.loads(urlread(idx_url).decode("utf-8"))
    return [PkgMeta.from_dict(p, repo.name, repo.priority) for p in j.get("packages",[])]

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
  manifest TEXT NOT NULL,
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
    c.execute("PRAGMA journal_mode=WAL"); c.executescript(SCHEMA)
    return c

def db_installed(conn) -> Dict[str,dict]:
    res={}
    for r in conn.execute("SELECT name,version,release,arch,provides,manifest FROM installed"):
        res[r[0]]={"version":r[1],"release":r[2],"arch":r[3],"provides":json.loads(r[4]),"manifest":json.loads(r[5])}
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

# =========================== CNF / SAT ========================================
class CNF:
    def __init__(self): 
        self.clauses: List[List[int]] = []
        self.next_var=1
        self.varname: Dict[int,str]={}
        self.namevar: Dict[str,int]={}
    def new_var(self, name:str) -> int:
        if name in self.namevar: return self.namevar[name]
        v=self.next_var; self.next_var+=1; self.namevar[name]=v; self.varname[v]=name; return v
    def add(self, *cl: Iterable[int]):
        for c in cl:
            c=list(c)
            if c: self.clauses.append(c)

class SATResult:
    def __init__(self, sat: bool, assign: Dict[int,bool]): 
        self.sat=sat; self.assign=assign

def dpll_solve(cnf: CNF, prefer_true: Set[int], prefer_false: Set[int]) -> SATResult:
    clauses = [list(c) for c in cnf.clauses]
    nvars = cnf.next_var-1
    assigns: Dict[int, Optional[bool]] = {i: None for i in range(1, nvars+1)}

    def unit_propagate() -> bool:
        changed=True
        while changed:
            changed=False
            for cl in clauses:
                sat=False; unassigned=[]
                for lit in cl:
                    v=abs(lit); val=assigns[v]
                    if val is None: unassigned.append(lit)
                    elif (val and lit>0) or ((not val) and lit<0): 
                        sat=True; break
                if sat: continue
                if not unassigned: return False
                if len(unassigned)==1:
                    lit=unassigned[0]; assigns[abs(lit)]=(lit>0); changed=True
        return True

    def choose_var() -> int:
        scores: Dict[int,int]={}
        for cl in clauses:
            satisfied=False
            for lit in cl:
                v=abs(lit); val=assigns[v]
                if val is None: scores[v]=scores.get(v,0)+1
                elif (val and lit>0) or ((not val) and lit<0): 
                    satisfied=True; break
            if satisfied: continue
        cand=[v for v,val in assigns.items() if val is None]
        if not cand: return 0
        cand.sort(key=lambda v:(v in prefer_true, -scores.get(v,0), -(v in prefer_false)))
        return cand[-1] if cand else 0

    def recurse() -> bool:
        if not unit_propagate(): return False
        if all(assigns[v] is not None for v in assigns): return True
        v = choose_var()
        if v==0: return True
        order=[True, False]
        if v in prefer_false and v not in prefer_true: order=[False, True]
        for val in order:
            saved=dict(assigns)
            assigns[v]=val
            if recurse(): return True
            assigns.update(saved)
        return False

    sat = recurse()
    final = {v: (assigns[v] if assigns[v] is not None else False) for v in assigns}
    return SATResult(sat, final)

# =========================== Resolver encoding =================================
def expr_to_cnf_disj(u: Universe, e: DepExpr, cnf: CNF, var_of: Dict[Tuple[str,str],int]) -> List[int]:
    if e.kind=="atom":
        lits=[var_of[(p.name,p.version)] for p in providers_for(u, e.atom)]
        return lits
    elif e.kind=="or":
        return list(set(expr_to_cnf_disj(u, e.left, cnf, var_of) + expr_to_cnf_disj(u, e.right, cnf, var_of)))
    else:
        die("expr_to_cnf_disj called on AND unexpectedly")

def encode_resolution(u: Universe, goals: List[DepExpr]) -> Tuple[CNF, Dict[Tuple[str,str],int], Set[int], Set[int]]:
    cnf = CNF()
    var_of: Dict[Tuple[str,str],int] = {}
    for name,lst in u.candidates_by_name.items():
        for p in lst: var_of[(p.name,p.version)] = cnf.new_var(f"{p.name}=={p.version}")
    # At-most-one per name
    for name,lst in u.candidates_by_name.items():
        for i in range(len(lst)):
            vi = var_of[(lst[i].name, lst[i].version)]
            for j in range(i+1,len(lst)):
                vj = var_of[(lst[j].name, lst[j].version)]
                cnf.add([-vi, -vj])

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
                if not disj: die("No provider for goal part")
                cnf.add(disj)
        else:
            disj = expr_to_cnf_disj(u, g, cnf, var_of)
            if not disj: die("No provider for goal")
            cnf.add(disj)

    return cnf, var_of, prefer_true, prefer_false

def solve(goals: List[str], universe: Universe) -> List[PkgMeta]:
    goal_exprs = [parse_dep_expr(s) for s in goals]
    cnf, var_of, ptrue, pfalse = encode_resolution(universe, goal_exprs)
    res = dpll_solve(cnf, ptrue, pfalse)
    if not res.sat: raise RuntimeError("Unsatisfiable dependency set")
    inv: Dict[int,Tuple[str,str]] = {v:k for k,v in var_of.items()}
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
def run_hook(hook: str, env: Dict[str,str]):
    path = HOOK_DIR / hook
    if path.exists() and os.access(path, os.X_OK):
        subprocess.run([str(path)], env={**os.environ, **env}, check=True)
        
# =========================== Service File Handling =============================
def handle_service_files(pkg_name: str, root: Path):
    """
    Detect service files from installed package and register them
    according to the active init system.
    """
    init = detect_init_system()
    policy = CONF.get("INIT_POLICY", "manual").lower()  # auto/manual/none

    if policy == "none":
        return

    if init == "systemd":
        service_dir = root / "usr/lib/systemd/system"
        if service_dir.exists():
            for svc in service_dir.glob("*.service"):
                if policy == "auto":
                    subprocess.run(["systemctl", "enable", "--now", svc.name],
                                   check=False)
                log(f"[systemd] Detected service: {svc.name}")

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
        
        
def remove_service_files(pkg_name: str, root: Path):
    """
    Handle service cleanup on package removal.
    """
    init = detect_init_system()
    policy = CONF.get("INIT_POLICY", "manual").lower()

    if policy == "none":
        return

    if init == "systemd":
        service_dir = root / "usr/lib/systemd/system"
        if service_dir.exists():
            for svc in service_dir.glob("*.service"):
                if policy == "auto":
                    subprocess.run(["systemctl", "disable", "--now", svc.name],
                                   check=False)
                log(f"[systemd] Disabled service: {svc.name}")

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

def collect_manifest(stagedir: Path) -> List[Dict[str,str]]:
    mani=[]
    for root,dirs,files in os.walk(stagedir):
        for fn in files:
            if fn in (".lpm-meta.json", ".lpm-manifest.json"):
                continue
            f=Path(root)/fn
            rel=f.relative_to(stagedir).as_posix()
            mani.append({
                "path":"/"+rel,
                "sha256":sha256sum(f),
                "size":f.stat().st_size
            })
    return sorted(mani,key=lambda e:e["path"])

    
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

    if shutil.which("zstd") is None:
        die("zstd is required to build .zst packages but was not found in PATH")

    # Collect manifest EXCLUDING meta/manifest files
    mani = []
    for root, dirs, files in os.walk(stagedir):
        for fn in files:
            if fn in (".lpm-meta.json", ".lpm-manifest.json"):
                continue
            f = Path(root) / fn
            rel = f.relative_to(stagedir).as_posix()
            mani.append({"path": "/" + rel, "sha256": sha256sum(f), "size": f.stat().st_size})
    mani.sort(key=lambda e: e["path"])

    # Write metadata + manifest *into stagedir*
    meta_path = stagedir / ".lpm-meta.json"
    mani_path = stagedir / ".lpm-manifest.json"
    meta_dict = dataclasses.asdict(meta)

    with meta_path.open("w", encoding="utf-8") as f:
        json.dump(meta_dict, f, indent=2, sort_keys=True)
    with mani_path.open("w", encoding="utf-8") as f:
        json.dump(mani, f, indent=2)

    # Package with tar + zstd
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
        for m in tqdm(tf, desc=f"Extracting {blob.name}", unit="file", colour="cyan"):
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

    # Run embedded install script if present
    script = root / ".lpm-install.sh"
    if script.exists() and os.access(script, os.X_OK):
        log(f"[lpm] Running embedded install script: {script}")
        subprocess.run([str(script)], check=False)

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
        for _ in tqdm(range(1), desc=f"Downloading {p.name}", colour="cyan"):
            data = urlread(url); dst.write_bytes(data)
        try:
            sig_url = url + ".sig"
            sig_data = urlread(sig_url)
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
        for fut in tqdm(as_completed(future_map), total=len(future_map), desc="Fetching", unit="pkg", colour="cyan"):
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

def do_install(pkgs: List[PkgMeta], root: Path, dry: bool, verify: bool, force: bool = False):
    global PROTECTED
    PROTECTED = load_protected()

    to_fetch = [p for p in pkgs if not (p.name in PROTECTED and not force)]
    downloads = fetch_all(to_fetch)

    def worker(p: PkgMeta):
        if p.name in PROTECTED and not force:
            warn(f"{p.name} is protected (from {PROTECTED_FILE}) and cannot be installed/upgraded without --force")
            return
        res = downloads.get(p.name)
        if isinstance(res, Exception):
            warn(f"Could not fetch {p.name} from repos ({res}), trying GitLab fallback...")
            tmp = Path(f"/tmp/lpm-dep-{p.name}.lpmbuild")
            fetch_lpmbuild(p.name, tmp)
            built = run_lpmbuild(tmp)
            installpkg(built, root=root, dry_run=dry, verify=verify, force=force)
        else:
            if res is None:
                blob, sig = fetch_blob(p)
            else:
                blob, sig = res
            installpkg(blob, root=root, dry_run=dry, verify=verify, force=force)

    max_workers = min(8, len(pkgs))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_map = {ex.submit(worker, p): p.name for p in pkgs}
        for fut in tqdm(as_completed(future_map), total=len(future_map), desc="Installing", unit="pkg", colour="cyan"):
            try:
                fut.result()
            except Exception as e:
                warn(f"install {future_map[fut]}: {e}")


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

    for f in tqdm(files, desc=f"Removing {name}", unit="file", colour="purple"):
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
            
        # Stop/disable/init cleanup for services
        remove_service_files(name, root)
 

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
        for fut in tqdm(as_completed(future_map), total=len(future_map), desc="Removing", unit="pkg", colour="purple"):
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
    except RuntimeError:
        warn("SAT solver failed to find upgrade set, falling back to GitLab fetch...")
        for dep in targets:
            built = build_from_gitlab(dep)
            installpkg(built, root=root, dry_run=dry, verify=verify, force=force)
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
                remove_service_files(p.name, Path(DEFAULT_ROOT))

        max_workers = min(8, len(upgrades))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(ex.map(svc_worker, upgrades))

    do_install(upgrades, root, dry, verify, force=force)


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
    bcmd = f"""
set -e
source "{script_path}"
_emit_scalar() {{
  n="$1"
  v="${{!1}}"
  printf "__SCALAR__ %s=%s\\n" "$n" "$v"
}}
_emit_array() {{
  n="$1"
  if declare -p "$n" 2>/dev/null | grep -q 'declare -a'; then
    eval "arr=(\\${{${{n}}[@]}})"
    printf "__ARRAY__ %s\\n" "$n"
    for x in "${{arr[@]}}"; do printf "%s\\0" "$x"; done
    printf "\\n"
  else
    printf "__ARRAY__ %s\\n" "$n"
    printf "\\n"
  fi
}}
for v in NAME VERSION RELEASE ARCH SUMMARY URL LICENSE CFLAGS; do _emit_scalar "$v"; done
for a in REQUIRES PROVIDES CONFLICTS OBSOLETES RECOMMENDS SUGGESTS; do _emit_array "$a"; done
"""

    try:
        proc = subprocess.run(["bash","-c", bcmd], capture_output=True, check=True)
    except subprocess.CalledProcessError as e:
        warn(f"lpmbuild parse failed: {e}")
        return {}, {}

    data = proc.stdout
    scalars: Dict[str,str] = {}
    arrays: Dict[str,List[str]] = {k: [] for k in ["REQUIRES","PROVIDES","CONFLICTS","OBSOLETES","RECOMMENDS","SUGGESTS"]}

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

    return scalars, arrays

def _maybe_fetch_source(url: str, dst_dir: Path):
    if not url: return
    fn = os.path.basename(urllib.parse.urlparse(url).path)
    if not fn: return
    dst = dst_dir / fn
    if dst.exists(): return
    ok(f"Fetching source: {url}")
    dst.write_bytes(urlread(url))

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
        data = urlread(url)
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
    built = run_lpmbuild(tmp, outdir=CACHE_DIR)

    # Copy to a stable cache filename
    if built != cache_pkg:
        try:
            shutil.copy2(built, cache_pkg)
        except Exception as e:
            warn(f"Failed to copy {built} to cache: {e}")
            return built
    return cache_pkg

def run_lpmbuild(script: Path, outdir: Optional[Path]=None) -> Path:
    script_path = script.resolve()
    script_dir = script_path.parent

    # --- Capture metadata first ---
    scal, arr = _capture_lpmbuild_metadata(script_path)
    name = scal.get("NAME", "")
    version = scal.get("VERSION", "")
    release = scal.get("RELEASE", "1")
    arch = scal.get("ARCH", ARCH)
    summary = scal.get("SUMMARY", "")
    url = scal.get("URL", "")
    license_ = scal.get("LICENSE", "")
    if not name or not version:
        die("lpmbuild missing NAME or VERSION")

    # --- Auto-build dependencies before continuing ---
    seen = set()
    deps_to_build: List[str] = []
    for dep in arr.get("REQUIRES", []):
        try:
            e = parse_dep_expr(dep)
        except Exception:
            continue
        parts = flatten_and(e) if e.kind == "and" else [e]
        for part in parts:
            if part.kind == "atom":
                depname = part.atom.name
                if depname in seen:
                    continue
                seen.add(depname)

                conn = db()
                installed = db_installed(conn)
                if depname not in installed:
                    deps_to_build.append(depname)

    def _build_dep(depname: str):
        log(f"[deps] building required package: {depname}")
        tmp = Path(f"/tmp/lpm-dep-{depname}.lpmbuild")
        fetch_lpmbuild(depname, tmp)
        run_lpmbuild(tmp, outdir or script_dir)

    if deps_to_build:
        max_workers = min(4, len(deps_to_build))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(tqdm(ex.map(_build_dep, deps_to_build), total=len(deps_to_build), desc="[deps] building", unit="pkg", colour="cyan"))

    stagedir = Path(f"/tmp/pkg-{name}")
    buildroot = Path(f"/tmp/build-{name}")
    srcroot   = Path(f"/tmp/src-{name}")

    for d in (stagedir, buildroot, srcroot):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)

    env = os.environ.copy()
    env.update({
        "DESTDIR": str(stagedir),
        "pkgdir": str(stagedir),
        "BUILDROOT": str(buildroot),
        "SRCROOT": str(srcroot),
    })

    base_flags = f"{OPT_LEVEL} -march={MARCH} -mtune={MTUNE} -pipe -fPIC"
    extra_cflags = " ".join(filter(None, [env.get("CFLAGS", "").strip(), scal.get("CFLAGS", "").strip()]))
    flags = f"{base_flags} {extra_cflags}".strip()
    env["CFLAGS"] = flags
    env["CXXFLAGS"] = flags
    env["LDFLAGS"] = OPT_LEVEL
    log(f"[opt] vendor={CPU_VENDOR} family={CPU_FAMILY} -> {flags}")

    # Auto-fetch source if URL provided
    _maybe_fetch_source(url, srcroot)

    # --- Run build functions inside sandbox ---
    def run_func(func: str, cwd: Path):
        sandboxed_run(func, cwd, env, script_path, stagedir, buildroot, srcroot)

    for fn in ("prepare", "build", "install"):
        try:
            run_func(fn, srcroot)
        except subprocess.CalledProcessError as e:
            die(f"{script.name}: function '{fn}' failed with code {e.returncode}")

    # --- Generate or capture install script ---
    install_sh = stagedir / ".lpm-install.sh"
    try:
        custom = subprocess.run(
            ["bash", "-c", f'source "{script_path}"; declare -f install_script'],
            capture_output=True, text=True
        )
        if custom.stdout.strip():
            log(f"[lpm] Embedding custom install_script() from {script.name}")
            with install_sh.open("w", encoding="utf-8") as f:
                f.write("#!/bin/sh\nset -e\n")
                f.write(custom.stdout)
                f.write("\ninstall_script \"$@\"\n")
            install_sh.chmod(0o755)
        else:
            with install_sh.open("w", encoding="utf-8") as f:
                f.write(f"""#!/bin/sh
set -e
echo "[lpm] Running default install script for {name}-{version}"
if command -v ldconfig >/dev/null 2>&1; then
    echo "[lpm] Running ldconfig"
    ldconfig || true
fi
exit 0
""")
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
    )

    outdir = script_dir if outdir is None else outdir
    out = outdir / f"{meta.name}-{meta.version}-{meta.release}.{meta.arch}{EXT}"
    build_package(stagedir, meta, out, sign=True)
    return out

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
    plan = solve(goals, u)
    log("[plan] install order:")
    for p in plan:
        log(f"  - {p.name}-{p.version}")
    if a.dry_run:
        return
    noverify = a.no_verify or os.environ.get("LPM_NO_VERIFY") == "1"

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
        do_install(plan, root, a.dry_run, verify=(not noverify), force=a.force)
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

def cmd_upgrade(a):
    root = Path(a.root or DEFAULT_ROOT)
    noverify = a.no_verify or os.environ.get("LPM_NO_VERIFY") == "1"
    dry = a.dry_run
    force = a.force

    u = build_universe()
    goals: List[str] = []
    if not a.names:
        for n, meta in u.installed.items():
            goals.append(f"{n} ~= {meta['version']}")
    else:
        goals += a.names

    try:
        plan = solve(goals, u)
    except RuntimeError:
        warn("SAT solver failed to find upgrade set, falling back to GitLab fetch...")
        for dep in a.names:
            built = build_from_gitlab(dep)
            installpkg(built, root=root, dry_run=dry, verify=(not noverify), force=force)
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
                remove_service_files(p.name, Path(DEFAULT_ROOT))
        max_workers = min(8, len(upgrades))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            list(ex.map(svc_worker, upgrades))

    try:
        do_install(upgrades, root, dry, verify=(not noverify), force=force)
    except SystemExit:
        if snapshot_id is not None:
            warn(f"Snapshot {snapshot_id} created at {snapshot_archive} for rollback.")
        raise

def cmd_list_installed(_):
    conn=db()
    for n,v,r,a in conn.execute("SELECT name,version,release,arch FROM installed ORDER BY name"):
        print(f"{n:30} {v}-{r}.{a}")

def cmd_history(_):
    conn=db()
    for ts,act,name,frm,to in conn.execute("SELECT ts,action,name,from_ver,to_ver FROM history ORDER BY id DESC LIMIT 200"):
        t=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        if act=="install": print(f"{t}  install  {name} -> {to}")
        else: print(f"{t}  remove   {name} ({frm})")

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
        for fut in tqdm(as_completed(futures), total=len(futures), desc="Verifying", unit="pkg", colour="cyan"):
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

def cmd_buildpkg(a):
    out = run_lpmbuild(a.script, a.outdir)
    if out and out.exists():
        # Bold purple output
        print(f"{PURPLE}[OK] Built {out}{RESET}", file=sys.stderr)
        print(f"{PURPLE}{out}{RESET}")
    else:
        die(f"Build failed for {a.script}")

def cmd_genindex(a):
    repo_dir = Path(a.repo_dir)
    gen_index(repo_dir, a.base_url, arch_filter=a.arch)

def cmd_fileremove(a):
    root = Path(a.root or DEFAULT_ROOT)

    def worker(name: str):
        removepkg(name=name, root=root, dry_run=a.dry_run, force=a.force)

    max_workers = min(8, len(a.names))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_map = {ex.submit(worker, n): n for n in a.names}
        for fut in tqdm(as_completed(future_map), total=len(future_map), desc="Removing", unit="pkg", colour="purple"):
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
        )

    max_workers = min(8, len(files))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_map = {ex.submit(worker, f): f for f in files}
        for fut in tqdm(as_completed(future_map), total=len(future_map), desc="Installing", unit="pkg", colour="cyan"):
            fut.result()

def installpkg(file: Path, root: Path = Path(DEFAULT_ROOT), dry_run: bool = False, verify: bool = True, force: bool = False):
    """
    Production-grade .zst package installer with protected package + dep resolution.
    """
    global PROTECTED
    PROTECTED = load_protected()

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
        return

    # --- Step 3c: Meta-package handler ---
    # If package has REQUIRES but no manifest payload → treat as meta-package
    if not mani or all(e["path"].startswith("/.lpm") for e in mani):
        if meta.requires:
            log(f"[meta] {meta.name} is a meta-package, resolving deps: {', '.join(meta.requires)}")
            u = build_universe()
            plan = solve(meta.requires, u)
            do_install(plan, root, dry_run, verify, force)
            ok(f"Installed meta-package {meta.name}-{meta.version}-{meta.release}.{meta.arch}")
            return


    # --- Step 4: Dry-run ---
    if dry_run:
        log(f"[dry-run] Would install {meta.name}-{meta.version}-{meta.release}.{meta.arch}")
        for e in mani:
            print(f" -> {e['path']} ({e['size']} bytes)")
        return

    # --- Step 5: Transaction (unchanged below) ---
    conn = db()
    with transaction(conn, f"install {meta.name}", dry_run):
        run_hook("pre_install", {
            "LPM_PKG": meta.name,
            "LPM_VERSION": meta.version,
            "LPM_ROOT": str(root),
        })

        tmp_root = Path(tempfile.mkdtemp(prefix=f"lpm-{meta.name}-", dir="/tmp"))
        try:
            manifest = extract_tar(file, tmp_root)

            # Validate manifest files
            for e in mani:
                f = tmp_root / e["path"].lstrip("/")
                if not f.exists():
                    die(f"Manifest missing file: {e['path']}")
                h = sha256sum(f)
                if h != e["sha256"]:
                    die(f"Hash mismatch for {e['path']}: expected {e['sha256']}, got {h}")

            # Move into root w/ conflict handling (same as before) ...
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
                    while True:
                        resp = input(f"[conflict] {rel} exists. [R]eplace / [S]kip / [A]bort? ").strip().lower()
                        if resp in ("r", "replace"):
                            if dest.is_file() or dest.is_symlink():
                                dest.unlink()
                            elif dest.is_dir():
                                shutil.rmtree(dest)
                            break
                        elif resp in ("s", "skip"):
                            log(f"[skip] {rel}")
                            src.unlink(missing_ok=True)
                            continue
                        elif resp in ("a", "abort"):
                            die(f"Aborted install due to conflict at {rel}")
                        else:
                            print("Please enter R, S, or A.")

                shutil.move(str(src), str(dest))

            # Update DB
            conn.execute(
                "REPLACE INTO installed(name,version,release,arch,provides,manifest,install_time) VALUES(?,?,?,?,?,?,?)",
                (
                    meta.name,
                    meta.version,
                    meta.release,
                    meta.arch,
                    json.dumps([meta.name] + meta.provides),
                    json.dumps(mani),
                    int(time.time()),
                ),
            )
            conn.execute(
                "INSERT INTO history(ts,action,name,from_ver,to_ver,details) VALUES(?,?,?,?,?,?)",
                (int(time.time()), "install", meta.name, None, meta.version, json.dumps(dataclasses.asdict(meta))),
            )
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

        run_hook("post_install", {
            "LPM_PKG": meta.name,
            "LPM_VERSION": meta.version,
            "LPM_ROOT": str(root),
        })
        
        # New: init system service integration
        handle_service_files(meta.name, root)

    ok(f"Installed {meta.name}-{meta.version}-{meta.release}.{meta.arch}")


def removepkg(name: str, root: Path = Path(DEFAULT_ROOT), dry_run: bool = False, force: bool = False):
    global PROTECTED
    PROTECTED = load_protected()

    if name in PROTECTED and not force:
        warn(f"{name} is protected (from {PROTECTED_FILE}) and cannot be removed without --force")
        return

    conn = db()
    cur = conn.execute("SELECT version, manifest FROM installed WHERE name=?", (name,))
    row = cur.fetchone()
    if not row:
        warn(f"{name} not installed")
        return

    version, manifest_json = row
    meta = {"name": name, "version": version, "manifest": json.loads(manifest_json) if manifest_json else []}

    with transaction(conn, f"remove {name}", dry_run):
        run_hook("pre_remove", {"LPM_PKG": name, "LPM_ROOT": str(root)})
        _remove_installed_package(meta, root, dry_run, conn)
        run_hook("post_remove", {"LPM_PKG": name, "LPM_ROOT": str(root)})

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

# =========================== Argparse / main ==================================
def build_parser()->argparse.ArgumentParser:
    p=argparse.ArgumentParser(prog="lpm", description="Linux Package Manager with SAT solver, signatures, and .lpmbuild")
    sub=p.add_subparsers(dest="cmd", required=True)

    sp=sub.add_parser("repolist", help="Show configured repositories"); sp.set_defaults(func=cmd_repolist)
    sp=sub.add_parser("repoadd", help="Add a repository"); sp.add_argument("name"); sp.add_argument("url");                   sp.add_argument("--priority",type=int,default=10); sp.set_defaults(func=cmd_repoadd)
    sp=sub.add_parser("repodel", help="Remove a repository"); sp.add_argument("name"); sp.set_defaults(func=cmd_repodel)

    sp=sub.add_parser("search", help="Search packages"); sp.add_argument("patterns", nargs="*"); sp.set_defaults(func=cmd_search)
    sp=sub.add_parser("info", help="Show package info"); sp.add_argument("names", nargs="+"); sp.set_defaults(func=cmd_info)

    sp=sub.add_parser("install", help="Install packages")
    sp.add_argument("names", nargs="+")
    sp.add_argument("--root")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--no-verify", action="store_true", help="skip signature verification (DANGEROUS)")
    sp.set_defaults(func=cmd_install)

    sp=sub.add_parser("remove", help="Remove packages")
    sp.add_argument("names", nargs="+")
    sp.add_argument("--root")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--force", action="store_true", help="override protected package list")
    sp.set_defaults(func=cmd_remove)

    sp=sub.add_parser("upgrade", help="Upgrade packages (targets or all)")
    sp.add_argument("names", nargs="*")
    sp.add_argument("--root")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--no-verify", action="store_true", help="skip signature verification (DANGEROUS)")
    sp.add_argument("--force", action="store_true", help="override protected package list for install/upgrade")
    sp.set_defaults(func=cmd_upgrade)

    sp=sub.add_parser("list", help="List installed packages"); sp.set_defaults(func=cmd_list_installed)
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
    sp.set_defaults(func=cmd_build)

    sp=sub.add_parser("buildpkg", help=f"Build a {EXT} package from a .lpmbuild script")
    sp.add_argument("script", type=Path)
    sp.add_argument("--outdir", default=Path.cwd(), type=Path)
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
    
    sp = sub.add_parser("protected", help="Show or edit protected package list")
    sp.add_argument("action", choices=["list", "add", "remove"])
    sp.add_argument("names", nargs="*", help="package names (for add/remove)")
    sp.set_defaults(func=cmd_protected)


    return p

def main(argv=None):
    args=build_parser().parse_args(argv)
    args.func(args)

if __name__=="__main__":
    main()

