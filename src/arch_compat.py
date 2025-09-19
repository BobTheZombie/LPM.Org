from __future__ import annotations

import logging
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .config import ARCH_REPO_ENDPOINTS
from .fs import urlread

PACMAN_RE = re.compile(r"\b(pacman|libalpm)\b")
FUNC_RE = re.compile(r"(^|\n)\s*(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{", re.MULTILINE)


@dataclass
class PKGBuildInfo:
    name: str
    version: str
    release: str
    summary: str
    arch: List[str]
    url: str
    license: List[str]
    depends: List[str]
    makedepends: List[str]
    optdepends: List[str]
    provides: List[str]
    conflicts: List[str]
    replaces: List[str]
    source: List[str]
    epoch: Optional[str] = None

    def dependency_names(self) -> List[str]:
        seen: Dict[str, None] = {}
        for raw in self.depends + self.makedepends:
            name = normalize_dependency_name(raw)
            if name and name not in seen:
                seen[name] = None
        return list(seen.keys())


def normalize_dependency_name(dep: str) -> str:
    text = dep.strip().strip("'\"")
    if not text:
        return ""
    if ":" in text:
        text = text.split(":", 1)[0]
    for sep in (">=", "<=", "=", ">", "<"):
        if sep in text:
            text = text.split(sep, 1)[0]
            break
    match = re.match(r"[A-Za-z0-9@._+-]+", text)
    return match.group(0) if match else text


def _parse_assignments(text: str) -> Dict[str, str]:
    assignments: Dict[str, str] = {}
    current_key: Optional[str] = None
    buffer: List[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        if current_key:
            buffer.append(line)
            if line.endswith(")"):
                assignments[current_key] = " ".join(buffer)
                current_key = None
                buffer = []
            continue

        if "=" not in line or line.endswith("()"):
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith("(") and not value.endswith(")"):
            current_key = key
            buffer = [value]
            continue
        assignments[key] = value

    return assignments


def _parse_array(value: Optional[str]) -> List[str]:
    if not value:
        return []
    text = value.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    try:
        return shlex.split(text)
    except ValueError:
        return [item for item in text.replace("\n", " ").split(" ") if item]


def _parse_scalar(value: Optional[str]) -> str:
    if not value:
        return ""
    value = value.strip()
    if value.startswith("(") and value.endswith(")"):
        arr = _parse_array(value)
        return arr[0] if arr else ""
    try:
        parts = shlex.split(value)
    except ValueError:
        return value.strip("'\"")
    return parts[0] if parts else ""


def _extract_functions(text: str) -> Dict[str, str]:
    functions: Dict[str, str] = {}
    for match in FUNC_RE.finditer(text):
        name = match.group(2)
        start = match.end()
        depth = 1
        pos = start
        while pos < len(text) and depth > 0:
            ch = text[pos]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            pos += 1
        body = text[start:pos - 1].strip()
        functions[name] = body
    return functions


def _sanitize_body(body: str) -> str:
    cleaned: List[str] = []
    for line in body.splitlines():
        if PACMAN_RE.search(line):
            continue
        cleaned.append(line.rstrip())
    return "\n".join(ln for ln in cleaned if ln.strip())


def _format_scalar(name: str, value: str) -> str:
    if value == "":
        return f"{name}="
    if re.match(r"^[A-Za-z0-9@._+-]+$", value):
        return f"{name}={value}"
    return f"{name}={shlex.quote(value)}"


def _format_array(name: str, values: Sequence[str]) -> str:
    if not values:
        return f"{name}=()"
    quoted = " ".join(shlex.quote(v) for v in values)
    return f"{name}=({quoted})"


def _format_function(name: str, body: str) -> str:
    lines = [f"{name}() {{"]
    body = body.strip("\n")
    if body:
        for ln in body.splitlines():
            if ln.strip():
                lines.append(f"    {ln.rstrip()}")
            else:
                lines.append("")
    else:
        lines.append("    :")
    lines.append("}")
    return "\n".join(lines)


def parse_pkgbuild(text: str) -> Tuple[PKGBuildInfo, Dict[str, str]]:
    assignments = _parse_assignments(text)
    functions = _extract_functions(text)

    pkgname_values = _parse_array(assignments.get("pkgname"))
    name = pkgname_values[0] if pkgname_values else _parse_scalar(assignments.get("pkgname"))
    pkgver = _parse_scalar(assignments.get("pkgver")) or "0"
    pkgrel = _parse_scalar(assignments.get("pkgrel")) or "1"
    epoch = _parse_scalar(assignments.get("epoch")) or None
    summary = _parse_scalar(assignments.get("pkgdesc"))
    arch = _parse_array(assignments.get("arch"))
    url = _parse_scalar(assignments.get("url"))
    license_ = _parse_array(assignments.get("license"))
    depends = _parse_array(assignments.get("depends"))
    makedepends = _parse_array(assignments.get("makedepends"))
    optdepends = _parse_array(assignments.get("optdepends"))
    provides = _parse_array(assignments.get("provides"))
    conflicts = _parse_array(assignments.get("conflicts"))
    replaces = _parse_array(assignments.get("replaces"))
    source = _parse_array(assignments.get("source"))

    info = PKGBuildInfo(
        name=name,
        version=pkgver,
        release=pkgrel,
        summary=summary,
        arch=arch,
        url=url,
        license=license_,
        depends=depends,
        makedepends=makedepends,
        optdepends=optdepends,
        provides=provides,
        conflicts=conflicts,
        replaces=replaces,
        source=source,
        epoch=epoch,
    )

    return info, functions


def convert_pkgbuild_to_lpmbuild(text: str) -> Tuple[PKGBuildInfo, str]:
    info, functions = parse_pkgbuild(text)

    version = info.version
    if info.epoch and info.epoch not in {"", "0"}:
        version = f"{info.epoch}:{version}"

    arch_value = ""
    if info.arch:
        first = info.arch[0]
        arch_value = "noarch" if first == "any" else first

    requires: List[str] = []
    seen: Dict[str, None] = {}
    for dep in info.depends + info.makedepends:
        if dep not in seen:
            seen[dep] = None
            requires.append(dep)

    suggests: List[str] = []
    for entry in info.optdepends:
        name = normalize_dependency_name(entry)
        if name:
            suggests.append(name)

    lines = ["# Generated by src.arch_compat", _format_scalar("NAME", info.name)]
    lines.append(_format_scalar("VERSION", version))
    lines.append(_format_scalar("RELEASE", info.release))
    if arch_value:
        lines.append(_format_scalar("ARCH", arch_value))
    if info.summary:
        lines.append(_format_scalar("SUMMARY", info.summary))
    if info.url:
        lines.append(_format_scalar("URL", info.url))
    if info.license:
        lines.append(_format_scalar("LICENSE", ", ".join(info.license)))
    if requires:
        lines.append(_format_array("REQUIRES", requires))
    if info.provides:
        lines.append(_format_array("PROVIDES", info.provides))
    if info.conflicts:
        lines.append(_format_array("CONFLICTS", info.conflicts))
    if info.replaces:
        lines.append(_format_array("OBSOLETES", info.replaces))
    if suggests:
        lines.append(_format_array("SUGGESTS", suggests))
    if info.source:
        lines.append(_format_array("SOURCE", info.source))

    lines.append("")

    prepare_body = _sanitize_body(functions.get("prepare", ""))
    build_body = _sanitize_body(functions.get("build", ""))
    check_body = _sanitize_body(functions.get("check", ""))
    install_body = _sanitize_body(functions.get("package", ""))

    lines.append(_format_function("prepare", prepare_body))
    lines.append("")
    lines.append(_format_function("build", build_body))
    if check_body:
        lines.append("")
        lines.append(_format_function("check", check_body))
    lines.append("")
    lines.append(_format_function("install", install_body))

    script = "\n".join(lines).rstrip() + "\n"
    return info, script


def _make_pkgbuild_url(base: str, pkgname: str) -> str:
    return f"{base.rstrip('/')}/{pkgname}/-/raw/main/PKGBUILD"


def fetch_pkgbuild(pkgname: str, endpoints: Optional[Mapping[str, str]] = None) -> str:
    endpoints = dict(endpoints or ARCH_REPO_ENDPOINTS)
    pkg = pkgname
    repo: Optional[str] = None
    if "/" in pkgname:
        repo, pkg = pkgname.split("/", 1)

    order: List[Tuple[str, str]] = []
    if repo and repo in endpoints:
        order.append((repo, endpoints[repo]))
    for key, base in endpoints.items():
        if repo and key == repo:
            continue
        order.append((key, base))

    errors: List[str] = []
    for label, base in order:
        url = _make_pkgbuild_url(base, pkg)
        try:
            data = urlread(url)
            return data.decode("utf-8")
        except Exception as exc:  # pragma: no cover - errors logged
            errors.append(f"{label}: {exc}")
            continue
    raise RuntimeError(f"Unable to fetch PKGBUILD for {pkgname} (tried {', '.join(label for label, _ in order)})")


class PKGBuildConverter:
    def __init__(
        self,
        workspace: Path,
        *,
        endpoints: Optional[Mapping[str, str]] = None,
        fetcher: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.endpoints = dict(endpoints or ARCH_REPO_ENDPOINTS)
        self._fetcher = fetcher
        self._scripts: Dict[str, Path] = {}
        self._meta: Dict[str, PKGBuildInfo] = {}
        self._in_progress: set[str] = set()

    def convert_text(self, text: str) -> Tuple[PKGBuildInfo, Path]:
        info, script = convert_pkgbuild_to_lpmbuild(text)
        path = self.workspace / f"{info.name}.lpmbuild"
        path.write_text(script, encoding="utf-8")
        self._scripts[info.name] = path
        self._meta[info.name] = info
        return info, path

    def convert_file(self, path: Path) -> Tuple[PKGBuildInfo, Path]:
        return self.convert_text(path.read_text(encoding="utf-8"))

    def _fetch_remote(self, pkgname: str) -> str:
        if self._fetcher is not None:
            return self._fetcher(pkgname)
        return fetch_pkgbuild(pkgname, self.endpoints)

    def convert_remote(self, pkgname: str) -> Tuple[PKGBuildInfo, Path]:
        text = self._fetch_remote(pkgname)
        info, path = self.convert_text(text)
        for dep in info.dependency_names():
            if dep != info.name:
                self.ensure_dependency(dep)
        return info, path

    def ensure_dependency(self, dep: str) -> Optional[Path]:
        base = normalize_dependency_name(dep)
        if not base:
            return None
        if base in self._scripts:
            return self._scripts[base]
        if base in self._in_progress:
            return None
        self._in_progress.add(base)
        try:
            text = self._fetch_remote(base)
        except Exception as exc:
            logging.warning("Failed to fetch dependency %s: %s", base, exc)
            self._in_progress.discard(base)
            return None
        try:
            info, path = self.convert_text(text)
        finally:
            self._in_progress.discard(base)
        for child in info.dependency_names():
            if child != info.name:
                self.ensure_dependency(child)
        return path

    def make_fetcher(self) -> Callable[[str, Path], Path]:
        def _fetch(pkgname: str, dest: Path) -> Path:
            base = normalize_dependency_name(pkgname) or pkgname
            path = self._scripts.get(base)
            if path is None:
                path = self.ensure_dependency(base)
            if path is None:
                raise RuntimeError(f"Unable to convert dependency {pkgname}")
            if path.resolve() == dest.resolve():
                return dest
            shutil.copy2(path, dest)
            return dest

        return _fetch

    def metadata_for(self, name: str) -> Optional[PKGBuildInfo]:
        return self._meta.get(name)

    def iter_scripts(self) -> Iterable[tuple[str, Path]]:
        """Yield ``(name, path)`` pairs for all converted packages."""

        for name, path in self._scripts.items():
            yield name, path


__all__ = [
    "PKGBuildInfo",
    "PKGBuildConverter",
    "convert_pkgbuild_to_lpmbuild",
    "fetch_pkgbuild",
    "normalize_dependency_name",
    "parse_pkgbuild",
]
