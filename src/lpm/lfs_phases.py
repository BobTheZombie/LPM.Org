from __future__ import annotations

import hashlib
import json
import os
import pwd
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable, Iterable


PHASE_ORDER = (
    "cross-toolchain",
    "temporary-tools",
    "chroot-tools",
    "final-system",
    "system-configuration",
    "boot",
)

DEFAULT_SOURCE_MIRRORS = (
    "https://ftp.osuosl.org/pub/lfs/lfs-packages",
    "https://lfs.gnlug.org/pub/lfs/lfs-packages",
    "https://mirror.download.it/lfs/pub/lfs-packages",
)


def _source_candidates(
    original: str, filename: str, version: str, mirrors: Iterable[str]
) -> list[str]:
    release = version.removesuffix("-systemd")
    candidates = [original]
    candidates.extend(
        f"{mirror.rstrip('/')}/{release}/{filename}" for mirror in mirrors
    )
    return list(dict.fromkeys(candidates))


def prepare_sources(
    *,
    wget_list: Path,
    md5sums: Path,
    destination: Path,
    version: str,
    offline: bool = False,
    mirrors: Iterable[str] = DEFAULT_SOURCE_MIRRORS,
) -> dict[str, object]:
    """Download the book's source set and verify every listed MD5 digest."""
    expected: dict[str, str] = {}
    for line in md5sums.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 32:
            expected[Path(parts[-1].lstrip("*")).name] = parts[0].lower()
    urls = [
        line.strip() for line in wget_list.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    destination.mkdir(parents=True, exist_ok=True)
    downloaded: list[str] = []
    verified: list[str] = []
    for url in urls:
        filename = Path(url.split("?", 1)[0]).name
        if not filename:
            raise ValueError(f"cannot determine filename from LFS source URL: {url}")
        path = destination / filename
        wanted = expected.get(filename)
        if not wanted:
            raise RuntimeError(f"LFS wget-list entry has no checksum: {filename}")

        def valid() -> bool:
            if not path.is_file() or not wanted:
                return False
            digest = hashlib.md5()  # nosec B324 - upstream LFS compatibility digest
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest() == wanted

        if not valid():
            if offline:
                raise FileNotFoundError(f"missing or invalid cached LFS source: {path}")
            temporary = path.with_name(f".{path.name}.part")
            failures: list[str] = []
            for candidate in _source_candidates(url, filename, version, mirrors):
                temporary.unlink(missing_ok=True)
                print(f"[jhalfs] downloading {filename} from {candidate}")
                try:
                    with urllib.request.urlopen(candidate, timeout=120) as response, temporary.open("wb") as out:
                        while chunk := response.read(1024 * 1024):
                            out.write(chunk)
                    digest = hashlib.md5()  # nosec B324 - upstream LFS digest
                    with temporary.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    if digest.hexdigest() != wanted:
                        failures.append(f"{candidate}: checksum mismatch")
                        continue
                    os.replace(temporary, path)
                    break
                except (OSError, urllib.error.URLError) as exc:
                    failures.append(f"{candidate}: {exc}")
                finally:
                    temporary.unlink(missing_ok=True)
            else:
                raise RuntimeError(
                    f"unable to download verified LFS source {filename}:\n  "
                    + "\n  ".join(failures)
                )
            downloaded.append(filename)
        if not valid():
            raise RuntimeError(f"LFS source checksum mismatch: {filename}")
        verified.append(filename)
    missing_digests = sorted(set(expected) - set(verified))
    if missing_digests:
        raise RuntimeError(
            "wget-list did not provide files listed by md5sums: "
            + ", ".join(missing_digests)
        )
    return {"destination": str(destination), "downloaded": downloaded, "verified": verified}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        name = tmp.name
    os.replace(name, path)


def load_phase_plan(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("format") != "lpm-lfs-phase-plan":
        raise ValueError(f"not an LPM LFS phase plan: {path}")
    if payload.get("format_version") != 1:
        raise ValueError(f"unsupported LFS phase-plan version: {payload.get('format_version')}")
    if not isinstance(payload.get("sections"), list):
        raise ValueError("LFS phase plan has no sections array")
    return payload


def _selected_phases(phases: Iterable[str]) -> tuple[str, ...]:
    selected = tuple(dict.fromkeys(str(p).strip() for p in phases if str(p).strip()))
    unknown = sorted(set(selected) - set(PHASE_ORDER))
    if unknown:
        raise ValueError(f"unknown LFS phase(s): {', '.join(unknown)}")
    return selected or PHASE_ORDER


def _command(
    *, target: Path, script: Path, context: str, lfs_user: str, env: dict[str, str]
) -> tuple[list[str], dict[str, str] | None, Path]:
    sources = target / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    if context == "lfs-user":
        try:
            account = pwd.getpwnam(lfs_user)
        except KeyError as exc:
            raise RuntimeError(
                f"LFS execution user {lfs_user!r} does not exist; create it before running phases"
            ) from exc
        for directory in (target, sources, target / "tools"):
            directory.mkdir(parents=True, exist_ok=True)
            os.chown(directory, account.pw_uid, account.pw_gid)
        command = ["runuser", "-u", lfs_user, "--", "/usr/bin/env", "-i"]
        command.extend((f"HOME={account.pw_dir}", f"TERM={os.environ.get('TERM', 'xterm')}"))
        command.extend(f"{key}={value}" for key, value in env.items())
        command.extend(["/bin/bash", str(script)])
        return command, None, sources
    if context == "chroot":
        try:
            relative = script.resolve().relative_to(target.resolve())
        except ValueError as exc:
            raise ValueError(f"chroot script is outside target: {script}") from exc
        chroot_env = {
            "HOME": "/root",
            "TERM": os.environ.get("TERM", "xterm"),
            "PS1": "(lfs chroot) \\u:\\w\\$ ",
            "PATH": "/usr/bin:/usr/sbin:/bin:/sbin:/tools/bin",
            "LC_ALL": "POSIX",
            "CONFIG_SITE": "/usr/share/config.site",
            "MAKEFLAGS": f"-j{os.cpu_count() or 1}",
        }
        command = ["chroot", str(target), "/usr/bin/env", "-i"]
        command.extend(f"{key}={value}" for key, value in chroot_env.items())
        command.extend(["/bin/bash", f"/{relative.as_posix()}"])
        return command, None, Path("/")
    return ["/bin/bash", str(script)], {**os.environ, **env}, sources


def run_phase_plan(
    *,
    plan_path: Path,
    target: Path,
    phases: Iterable[str] = (),
    lfs_user: str = "lfs",
    resume: bool = False,
    dry_run: bool = False,
    allow_manual: bool = False,
    force: bool = False,
    run: Callable[..., subprocess.CompletedProcess[object]] = subprocess.run,
) -> dict[str, object]:
    plan = load_phase_plan(plan_path)
    selected = _selected_phases(phases)
    state_path = plan_path.parent / "execution-state.json"
    state: dict[str, object] = {"format": "lpm-lfs-execution-state", "completed": {}}
    if resume and state_path.is_file():
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            state = loaded
    prior_book = state.get("book_sha256")
    if prior_book and prior_book != plan.get("book_sha256"):
        raise RuntimeError("LFS execution state belongs to a different rendered book")
    completed = state.setdefault("completed", {})
    if not isinstance(completed, dict):
        raise ValueError("invalid LFS execution state: completed must be an object")

    machine = os.uname().machine
    env = {
        "LFS": str(target),
        "LFS_TGT": f"{machine}-lfs-linux-gnu",
        "LC_ALL": "POSIX",
        "PATH": f"{target}/tools/bin:/usr/bin:/bin",
        "MAKEFLAGS": f"-j{os.cpu_count() or 1}",
        "CONFIG_SITE": f"{target}/usr/share/config.site",
    }
    executed: list[str] = []
    skipped: list[str] = []
    completed_phases = state.setdefault("completed_phases", {})
    if not isinstance(completed_phases, dict):
        raise ValueError("invalid LFS execution state: completed_phases must be an object")
    sections = [item for item in plan["sections"] if isinstance(item, dict)]
    for phase in PHASE_ORDER:
        if phase not in selected:
            continue
        phase_index = PHASE_ORDER.index(phase)
        prerequisites = PHASE_ORDER[:phase_index]
        missing = [name for name in prerequisites if name not in completed_phases]
        # A phase selected in the same invocation is completed before the next
        # phase reaches this gate.
        missing = [name for name in missing if name not in selected]
        if missing and not force:
            raise RuntimeError(
                f"LFS phase {phase!r} requires completed phase(s): {', '.join(missing)}; "
                "run them first or use --force"
            )
        for raw in sections:
            if raw.get("phase") != phase:
                continue
            section_id = str(raw.get("section") or f"section-{raw.get('number')}")
            automatic = bool(raw.get("automatic"))
            if not automatic and not allow_manual:
                skipped.append(section_id)
                continue
            script = Path(str(raw["script"]))
            expected = str(raw["sha256"])
            if not script.is_file() or _sha256(script) != expected:
                raise RuntimeError(f"LFS section script failed integrity check: {script}")
            prior = completed.get(section_id)
            if resume and isinstance(prior, dict) and prior.get("sha256") == expected:
                skipped.append(section_id)
                continue
            command, command_env, cwd = _command(
                target=target, script=script, context=str(raw["context"]),
                lfs_user=lfs_user, env=env,
            )
            if dry_run:
                print(f"[jhalfs][dry-run] phase={phase} section={section_id}: {' '.join(command)}")
                continue
            run(command, check=True, cwd=cwd, env=command_env)
            completed[section_id] = {
                "sha256": expected, "phase": phase, "completed_at": int(time.time()),
            }
            state["book_sha256"] = plan["book_sha256"]
            state["selected_phases"] = list(selected)
            _write_json(state_path, state)
            executed.append(section_id)
        if not dry_run:
            completed_phases[phase] = {"completed_at": int(time.time())}
            _write_json(state_path, state)
    return {
        "state": str(state_path),
        "selected_phases": list(selected),
        "executed": executed,
        "skipped": skipped,
    }


__all__ = [
    "DEFAULT_SOURCE_MIRRORS",
    "PHASE_ORDER",
    "load_phase_plan",
    "prepare_sources",
    "run_phase_plan",
]
