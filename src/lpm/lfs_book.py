from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.request
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Optional


DEFAULT_BOOK_VERSION = "13.1-systemd"
DEFAULT_BOOK_BASE = "https://www.linuxfromscratch.org/lfs/downloads"
DEFAULT_BOOK_VIEW_BASE = "https://www.linuxfromscratch.org/lfs/view"


@dataclass(frozen=True)
class BuildInstruction:
    number: int
    section: str
    title: str
    command: str
    script: str
    sha256: str


@dataclass(frozen=True)
class BuildSection:
    number: int
    section: str
    title: str
    chapter: int
    phase: str
    context: str
    commands: tuple[str, ...]
    script: str
    sha256: str
    automatic: bool
    reason: str = ""


PHASES: tuple[dict[str, object], ...] = (
    {"name": "cross-toolchain", "chapters": (5,), "context": "lfs-user"},
    {"name": "temporary-tools", "chapters": (6,), "context": "lfs-user"},
    {"name": "chroot-tools", "chapters": (7,), "context": "chroot"},
    {"name": "final-system", "chapters": (8,), "context": "chroot"},
    {"name": "system-configuration", "chapters": (9,), "context": "chroot"},
    {"name": "boot", "chapters": (10,), "context": "chroot"},
)


def _chapter(section: str, title: str) -> int:
    match = re.search(r"chapter(?:0?)(\d+)", section, re.IGNORECASE)
    if not match:
        match = re.match(r"\s*(\d+)\.", title)
    return int(match.group(1)) if match else 0


def _phase_for(chapter: int) -> tuple[str, str]:
    for phase in PHASES:
        if chapter in phase["chapters"]:
            return str(phase["name"]), str(phase["context"])
    return "manual", "host-root"


def _context_for(section: str, chapter: int, default: str) -> str:
    lowered = section.lower()
    if chapter == 7 and any(
        marker in lowered
        for marker in (
            "changingowner", "ownership", "bindmount", "kernfs", "virtual", "chroot",
        )
    ):
        return "host-root"
    return default


def _automation_policy(section: str, chapter: int) -> tuple[bool, str]:
    if chapter not in {5, 6, 7, 8, 9, 10}:
        return False, "outside executable LFS build chapters"
    lowered = section.lower()
    # Chapter 8's package-management discussion contains illustrative shell
    # snippets (for example, grepping for processes using deleted libraries).
    # They are neither build steps nor reliable in a pristine chroot.
    if chapter == 8 and (
        lowered.startswith("pkgmgmt") or lowered.startswith("ch-system-pkgmgt")
    ):
        return False, "package-management documentation, not a build section"
    # These sections change the execution boundary itself.  The bootstrap
    # runner owns those operations and must not recursively chroot or mount.
    if chapter == 7 and any(
        marker in lowered for marker in ("bindmount", "kernfs", "virtual", "chroot")
    ):
        return False, "execution-boundary operation is managed by lpm"
    if any(marker in lowered for marker in ("setrootpassword", "root-password")):
        return False, "interactive password configuration"
    return True, ""


_SHELL_REFRESH_RE = re.compile(
    r"^\s*exec\s+/(?:usr/)?bin/(?:ba)?sh\s+(?:--login|-l)\s*$"
)


def _normalize_automatic_command(command: str) -> str:
    """Make book commands safe for unattended, resumable execution.

    The Chapter 7 command exists to refresh name resolution in a human-driven
    chroot session.  An automated section already runs in a fresh process, and
    allowing ``exec bash --login`` would replace the section runner and hide
    every command that follows it.

    LFS also uses verbose symbolic-link commands that intentionally assume a
    pristine tree.  A section-level resume may replay those commands, so add
    force/no-dereference while preserving all other short options.
    """
    lines: list[str] = []
    for line in command.splitlines():
        if _SHELL_REFRESH_RE.fullmatch(line):
            continue
        match = re.match(r"^(\s*)ln\s+-([A-Za-z]*s[A-Za-z]*)\s+", line)
        if match:
            options = match.group(2)
            for flag in "fn":
                if flag not in options:
                    options += flag
            line = f"{match.group(1)}ln -{options} " + line[match.end():]
        lines.append(line)
    return "\n".join(lines).strip()


class _BookParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.heading_depth = 0
        self.heading_tag = ""
        self.heading_parts: list[str] = []
        self.current_title = "LFS build instruction"
        self.current_section = ""
        self.pre_depth = 0
        self.pre_parts: list[str] = []
        self.pre_is_command = False
        self.instructions: list[tuple[str, str, str]] = []

    @staticmethod
    def _classes(attrs: list[tuple[str, Optional[str]]]) -> set[str]:
        value = dict(attrs).get("class") or ""
        return {item for item in value.split() if item}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        attrs_dict = dict(attrs)
        # The rendered LFS no-chunks book places section identifiers on an
        # empty anchor inside each heading (``<h2><a id=...></a>...``), not on
        # the heading element itself.  A complete execution unit is the
        # top-level h2 page/package section.  Nested h3/h4 IDs (often anonymous
        # values such as ``id6927``) describe steps within that package and
        # must not split configure/build/install into separate scripts.
        heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
        section_heading_tags = {"h1", "h2"}
        if attrs_dict.get("id") and (
            tag in section_heading_tags
            or (self.heading_depth and self.heading_tag in section_heading_tags)
        ):
            self.current_section = str(attrs_dict["id"])
        if tag in heading_tags:
            self.heading_depth = 1
            self.heading_tag = tag
            self.heading_parts = []
            return
        if self.heading_depth:
            self.heading_depth += 1

        if tag == "pre":
            self.pre_depth = 1
            self.pre_parts = []
            classes = self._classes(attrs)
            self.pre_is_command = "userinput" in classes
            return
        if self.pre_depth:
            self.pre_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self.heading_depth:
            self.heading_depth -= 1
            if self.heading_depth == 0:
                title = " ".join("".join(self.heading_parts).split())
                # Package sections contain nested unnumbered headings such as
                # "Note", "Caution", and "Installation of ...".  Retain the
                # latest numbered book heading so phase classification still
                # has its chapter number when a command block is encountered.
                if title and (re.match(r"\d+(?:\.\d+)+\.?\s", title) or not self.current_title):
                    self.current_title = title
                self.heading_tag = ""

        if self.pre_depth:
            self.pre_depth -= 1
            if self.pre_depth == 0 and self.pre_is_command:
                command = "".join(self.pre_parts).replace("\xa0", " ").strip()
                if command:
                    self.instructions.append(
                        (self.current_section, self.current_title, command)
                    )
                self.pre_parts = []
                self.pre_is_command = False

    def handle_data(self, data: str) -> None:
        if self.heading_depth:
            self.heading_parts.append(data)
        if self.pre_depth and self.pre_is_command:
            self.pre_parts.append(data)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def _atomic_text(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    tmp_path.chmod(mode)
    os.replace(tmp_path, path)


def _download(url: str, destination: Path) -> None:
    maximum_size = 64 * 1024 * 1024
    destination.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("wb", dir=destination.parent, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                announced = int(response.headers.get("Content-Length", "0") or 0)
                if announced > maximum_size:
                    raise RuntimeError(f"refusing oversized LFS book resource: {url}")
                downloaded = 0
                while chunk := response.read(1024 * 1024):
                    downloaded += len(chunk)
                    if downloaded > maximum_size:
                        raise RuntimeError(f"refusing oversized LFS book resource: {url}")
                    tmp.write(chunk)
            tmp.flush()
            os.fsync(tmp.fileno())
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise
    os.replace(tmp_path, destination)


def _version_release(version: str) -> str:
    match = re.fullmatch(r"(\d+\.\d+)-systemd", version)
    if not match:
        raise ValueError("book version must use RELEASE-systemd, for example 13.1-systemd")
    return match.group(1)


def default_book_url(version: str) -> str:
    release = _version_release(version)
    return f"{DEFAULT_BOOK_BASE}/{version}/LFS-BOOK-{release}-NOCHUNKS.html"


def default_support_url(version: str, filename: str) -> str:
    _version_release(version)
    remote_filename = "wget-list-systemd" if filename == "wget-list" else filename
    return f"{DEFAULT_BOOK_VIEW_BASE}/{version}/{remote_filename}"


def cache_book(
    *,
    version: str,
    cache_dir: Path,
    url: Optional[str] = None,
    expected_sha256: Optional[str] = None,
    offline: bool = False,
    refresh: bool = False,
) -> dict[str, object]:
    release = _version_release(version)
    if expected_sha256 and not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256):
        raise ValueError("book SHA-256 must contain exactly 64 hexadecimal characters")
    expected_sha256 = expected_sha256.lower() if expected_sha256 else None
    source_url = url or default_book_url(version)
    version_cache = Path(cache_dir) / version
    version_cache.mkdir(parents=True, exist_ok=True)
    book_path = version_cache / f"LFS-BOOK-{release}-NOCHUNKS.html"
    metadata_path = version_cache / "cache.json"

    prior: dict[str, object] = {}
    if metadata_path.is_file():
        try:
            prior = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            prior = {}

    expected = expected_sha256
    if not expected and prior.get("version") == version and prior.get("url") == source_url:
        expected = str(prior.get("sha256") or "") or None

    cache_valid = book_path.is_file()
    if cache_valid and expected:
        cache_valid = _sha256(book_path) == expected
    if refresh:
        cache_valid = False
    if not cache_valid:
        if offline:
            raise FileNotFoundError(f"no valid cached LFS book for {version}: {book_path}")
        _download(source_url, book_path)

    actual_sha256 = _sha256(book_path)
    if expected and actual_sha256 != expected:
        book_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"LFS book checksum mismatch: expected {expected}, got {actual_sha256}"
        )

    support: dict[str, dict[str, str]] = {}
    for filename in ("wget-list", "md5sums"):
        path = version_cache / filename
        support_url = default_support_url(version, filename)
        prior_support = prior.get("support")
        prior_entry = (
            prior_support.get(filename, {})
            if isinstance(prior_support, dict)
            else {}
        )
        prior_digest = (
            str(prior_entry.get("sha256") or "")
            if isinstance(prior_entry, dict)
            else ""
        )
        prior_url = (
            str(prior_entry.get("url") or "")
            if isinstance(prior_entry, dict)
            else ""
        )
        # A prior LPM release cached the SysV wget-list under this local
        # filename.  URL identity is part of cache validity so it is replaced
        # automatically with wget-list-systemd on the next prepare/resume.
        same_source = prior_url == support_url
        if not same_source:
            prior_digest = ""
        support_valid = path.is_file() and same_source
        if support_valid and prior_digest:
            support_valid = _sha256(path) == prior_digest
        if refresh or not support_valid:
            if offline:
                raise FileNotFoundError(f"no valid cached LFS support file: {path}")
            else:
                _download(support_url, path)
        actual_support_digest = _sha256(path)
        if prior_digest and actual_support_digest != prior_digest:
            path.unlink(missing_ok=True)
            raise RuntimeError(
                f"LFS support checksum mismatch for {filename}: "
                f"expected {prior_digest}, got {actual_support_digest}"
            )
        support[filename] = {
            "path": str(path),
            "sha256": actual_support_digest,
            "url": support_url,
        }

    result: dict[str, object] = {
        "version": version,
        "url": source_url,
        "path": str(book_path),
        "sha256": actual_sha256,
        "support": support,
    }
    _atomic_json(metadata_path, result)
    return result


def extract_build_instructions(book_path: Path, output_dir: Path) -> dict[str, object]:
    parser = _BookParser()
    parser.feed(Path(book_path).read_text(encoding="utf-8"))
    parser.close()
    if not parser.instructions:
        raise RuntimeError(f"no build instruction blocks found in {book_path}")

    scripts_dir = Path(output_dir) / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    for old_script in scripts_dir.glob("*.sh"):
        old_script.unlink()

    records: list[BuildInstruction] = []
    for number, (section, title, command) in enumerate(parser.instructions, start=1):
        slug_source = section or title
        slug = re.sub(r"[^a-z0-9]+", "-", slug_source.lower()).strip("-")
        slug = slug[:64] or "instruction"
        filename = f"{number:04d}-{slug}.sh"
        body = "#!/bin/bash\nset -e\n\n" + command.rstrip() + "\n"
        script_path = scripts_dir / filename
        _atomic_text(script_path, body, mode=0o755)
        records.append(
            BuildInstruction(
                number=number,
                section=section,
                title=title,
                command=command,
                script=str(script_path),
                sha256=_sha256(script_path),
            )
        )

    # A jhalfs execution unit is a complete book section, not an individual
    # <pre> block.  Keeping all blocks from a section in one shell preserves
    # cd/export state and matches how package build instructions are written.
    grouped: list[tuple[str, str, list[str]]] = []
    for section, title, command in parser.instructions:
        if grouped and grouped[-1][0] == section:
            grouped[-1][2].append(command)
        else:
            grouped.append((section, title, [command]))

    sections_dir = Path(output_dir) / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)
    for old_script in sections_dir.glob("*.sh"):
        old_script.unlink()

    sections: list[BuildSection] = []
    for number, (section, title, commands) in enumerate(grouped, start=1):
        chapter = _chapter(section, title)
        phase, default_context = _phase_for(chapter)
        context = _context_for(section, chapter, default_context)
        automatic, reason = _automation_policy(section, chapter)
        commands = [
            filtered for command in commands
            if (filtered := _normalize_automatic_command(command))
        ]
        slug_source = section or title
        slug = re.sub(r"[^a-z0-9]+", "-", slug_source.lower()).strip("-")
        filename = f"{number:04d}-{(slug[:72] or 'section')}.sh"
        body = (
            "#!/bin/bash\n"
            "set -euo pipefail\n\n"
            "umask 022\n\n"
            + "\n\n".join(command.rstrip() for command in commands)
            + "\n"
        )
        script_path = sections_dir / filename
        _atomic_text(script_path, body, mode=0o755)
        sections.append(
            BuildSection(
                number=number,
                section=section,
                title=title,
                chapter=chapter,
                phase=phase,
                context=context,
                commands=tuple(commands),
                script=str(script_path),
                sha256=_sha256(script_path),
                automatic=automatic,
                reason=reason,
            )
        )

    index = {
        "format": "lpm-lfs-instructions",
        "format_version": 1,
        "book": str(Path(book_path)),
        "book_sha256": _sha256(Path(book_path)),
        "instruction_count": len(records),
        "instructions": [asdict(record) for record in records],
    }
    index_path = Path(output_dir) / "instructions.json"
    _atomic_json(index_path, index)
    phase_plan = {
        "format": "lpm-lfs-phase-plan",
        "format_version": 1,
        "book": str(Path(book_path)),
        "book_sha256": index["book_sha256"],
        "phases": list(PHASES),
        "section_count": len(sections),
        "sections": [asdict(record) for record in sections],
    }
    plan_path = Path(output_dir) / "phase-plan.json"
    _atomic_json(plan_path, phase_plan)
    return {
        **index,
        "index": str(index_path),
        "scripts_dir": str(scripts_dir),
        "phase_plan": str(plan_path),
        "sections_dir": str(sections_dir),
        "section_count": len(sections),
    }


def prepare_book(
    *,
    version: str = DEFAULT_BOOK_VERSION,
    cache_dir: Path,
    output_dir: Path,
    url: Optional[str] = None,
    expected_sha256: Optional[str] = None,
    offline: bool = False,
    refresh: bool = False,
) -> dict[str, object]:
    cached = cache_book(
        version=version,
        cache_dir=cache_dir,
        url=url,
        expected_sha256=expected_sha256,
        offline=offline,
        refresh=refresh,
    )
    extracted = extract_build_instructions(Path(str(cached["path"])), output_dir)
    return {"cache": cached, "extracted": extracted}


__all__ = [
    "BuildInstruction",
    "BuildSection",
    "DEFAULT_BOOK_VERSION",
    "PHASES",
    "cache_book",
    "default_book_url",
    "extract_build_instructions",
    "prepare_book",
]
