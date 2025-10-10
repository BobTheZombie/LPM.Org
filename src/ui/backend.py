"""High level helpers for the Tk based LPM UI."""
from __future__ import annotations

import fnmatch
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Mapping, Sequence

from src.lpm.app import PkgMeta, db, load_universe


@dataclass(frozen=True)
class PackageSummary:
    """Lightweight summary for a package available in a repository."""

    name: str
    version: str
    release: str
    arch: str
    summary: str
    repo: str

    @property
    def display_version(self) -> str:
        return f"{self.version}-{self.release}.{self.arch}"


@dataclass(frozen=True)
class PackageDetails(PackageSummary):
    """Detailed package metadata for display in the UI."""

    homepage: str
    license: str
    provides: Sequence[str]
    requires: Sequence[str]
    conflicts: Sequence[str]
    obsoletes: Sequence[str]
    recommends: Sequence[str]
    suggests: Sequence[str]
    blob: str | None


@dataclass(frozen=True)
class InstalledPackage:
    """Representation of an installed package."""

    name: str
    version: str
    release: str
    arch: str
    installed_at: str
    origin: str

    @property
    def display_version(self) -> str:
        return f"{self.version}-{self.release}.{self.arch}"


class LPMBackend:
    """Facilitates read/write operations for the graphical UI."""

    def __init__(self, universe_ttl: int = 60):
        self._lock = threading.Lock()
        self._universe: Mapping[str, List[PkgMeta]] | None = None
        self._universe_timestamp: float = 0.0
        self._universe_ttl = universe_ttl

    # ------------------------------------------------------------------
    # Repository helpers
    def _refresh_universe(self, force: bool = False) -> Mapping[str, List[PkgMeta]]:
        with self._lock:
            now = time.time()
            if (
                not force
                and self._universe is not None
                and now - self._universe_timestamp < self._universe_ttl
            ):
                return self._universe
            universe = load_universe()
            self._universe = universe
            self._universe_timestamp = now
            return universe

    def search(self, pattern: str) -> List[PackageSummary]:
        """Return packages that match *pattern* (shell style wildcards)."""

        pattern = pattern or "*"
        universe = self._refresh_universe()
        matches: List[PackageSummary] = []
        for name, entries in universe.items():
            if not fnmatch.fnmatch(name, pattern):
                continue
            pkg = entries[0]
            matches.append(
                PackageSummary(
                    name=pkg.name,
                    version=pkg.version,
                    release=pkg.release,
                    arch=pkg.arch,
                    summary=pkg.summary or "",
                    repo=pkg.repo or "",
                )
            )
        matches.sort(key=lambda item: item.name)
        return matches

    def get_details(self, name: str) -> PackageDetails:
        universe = self._refresh_universe()
        entries = universe.get(name)
        if not entries:
            raise KeyError(name)
        pkg = entries[0]
        return PackageDetails(
            name=pkg.name,
            version=pkg.version,
            release=pkg.release,
            arch=pkg.arch,
            summary=pkg.summary or "",
            repo=pkg.repo or "",
            homepage=pkg.url or "",
            license=pkg.license or "",
            provides=tuple(pkg.provides),
            requires=tuple(pkg.requires),
            conflicts=tuple(pkg.conflicts),
            obsoletes=tuple(pkg.obsoletes),
            recommends=tuple(pkg.recommends),
            suggests=tuple(pkg.suggests),
            blob=pkg.blob,
        )

    def refresh_universe(self) -> List[PackageSummary]:
        """Force a metadata refresh and return all known packages."""

        self._refresh_universe(force=True)
        return self.search("*")

    # ------------------------------------------------------------------
    # Local system helpers
    def list_installed(self) -> List[InstalledPackage]:
        conn = db()
        try:
            rows = list(
                conn.execute(
                    "SELECT name,version,release,arch,install_time,explicit "
                    "FROM installed ORDER BY name"
                )
            )
        finally:
            conn.close()

        installed: List[InstalledPackage] = []
        for name, version, release, arch, ts, explicit in rows:
            installed.append(
                InstalledPackage(
                    name=name,
                    version=version,
                    release=release,
                    arch=arch,
                    installed_at=_format_install_time(ts),
                    origin="explicit" if explicit else "dependency",
                )
            )
        return installed

    # ------------------------------------------------------------------
    # Mutating operations
    def run_cli(self, args: Sequence[str], *, root: Path | None = None) -> subprocess.CompletedProcess[str]:
        """Invoke the CLI in a subprocess and capture its output."""

        cmd = [sys.executable, "-m", "src.lpm.app", *args]
        env = dict(os.environ)
        if root is not None:
            env.setdefault("LPM_ROOT", str(root))
        return subprocess.run(cmd, check=False, capture_output=True, text=True, env=env)

    def install(self, names: Iterable[str]) -> subprocess.CompletedProcess[str]:
        return self.run_cli(["install", *names])

    def remove(self, names: Iterable[str]) -> subprocess.CompletedProcess[str]:
        return self.run_cli(["remove", *names])

    def upgrade(self, names: Iterable[str] | None = None) -> subprocess.CompletedProcess[str]:
        args = ["upgrade"]
        if names:
            args.extend(names)
        return self.run_cli(args)


# ----------------------------------------------------------------------
def _format_install_time(timestamp: int | None) -> str:
    if not timestamp:
        return "unknown"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(timestamp)))
    except (ValueError, OverflowError, OSError):
        return "unknown"
