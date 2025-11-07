"""High level helpers for the PySide6-based LPM UI."""
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

from src import config as lpm_config
from src.lpm.app import PkgMeta, Repo, db, list_repos, load_universe, save_repos


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


@dataclass(frozen=True)
class Repository:
    """Representation of a configured binary repository."""

    name: str
    url: str
    priority: int
    bias: float
    decay: float


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
    # Repository helpers
    def list_repositories(self) -> List[Repository]:
        """Return configured repositories."""

        repos = list_repos()
        return [
            Repository(
                name=r.name,
                url=r.url,
                priority=r.priority,
                bias=r.bias,
                decay=r.decay,
            )
            for r in repos
        ]

    def add_repository(self, repo: Repository) -> None:
        """Persist a new repository configuration."""

        repos = list_repos()
        if any(existing.name == repo.name for existing in repos):
            raise ValueError(f"Repository '{repo.name}' already exists")
        repos.append(
            Repo(
                name=repo.name,
                url=repo.url,
                priority=repo.priority,
                bias=repo.bias,
                decay=repo.decay,
            )
        )
        save_repos(repos)
        self._refresh_universe(force=True)

    def update_repository(self, repo: Repository) -> None:
        """Update an existing repository entry."""

        repos = list_repos()
        for idx, existing in enumerate(repos):
            if existing.name == repo.name:
                repos[idx] = Repo(
                    name=repo.name,
                    url=repo.url,
                    priority=repo.priority,
                    bias=repo.bias,
                    decay=repo.decay,
                )
                save_repos(repos)
                self._refresh_universe(force=True)
                return
        raise KeyError(repo.name)

    def remove_repository(self, name: str) -> None:
        existing = list_repos()
        repos = [repo for repo in existing if repo.name != name]
        if len(repos) == len(existing):
            raise KeyError(name)
        save_repos(repos)
        self._refresh_universe(force=True)

    def ensure_lpmbuild_repository(self) -> Repository:
        """Ensure the default lpmbuild repository is present."""

        default_url = (
            lpm_config.CONF.get(
                "LPMBUILD_REPO",
                "https://gitlab.com/lpm-org/packages/-/raw/main/repo",
            )
            or "https://gitlab.com/lpm-org/packages/-/raw/main/repo"
        )
        repos = list_repos()
        for repo in repos:
            if repo.name == "lpmbuild":
                return Repository(
                    name=repo.name,
                    url=repo.url,
                    priority=repo.priority,
                    bias=repo.bias,
                    decay=repo.decay,
                )
        lpmbuild_repo = Repository(
            name="lpmbuild",
            url=default_url.rstrip("/"),
            priority=10,
            bias=1.0,
            decay=0.95,
        )
        repos.append(
            Repo(
                name=lpmbuild_repo.name,
                url=lpmbuild_repo.url,
                priority=lpmbuild_repo.priority,
                bias=lpmbuild_repo.bias,
                decay=lpmbuild_repo.decay,
            )
        )
        save_repos(repos)
        self._refresh_universe(force=True)
        return lpmbuild_repo

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

    def build_package(
        self,
        script_path: str,
        *,
        outdir: str | None = None,
        no_deps: bool = False,
        force_rebuild: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        args: list[str] = ["buildpkg", script_path]
        if outdir:
            args.extend(["--outdir", outdir])
        if no_deps:
            args.append("--no-deps")
        if force_rebuild:
            args.append("--force-rebuild")
        return self.run_cli(args)

    def install_local_packages(self, files: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if not files:
            raise ValueError("No package files provided")
        args = ["installpkg", *files]
        return self.run_cli(args)


# ----------------------------------------------------------------------
def _format_install_time(timestamp: int | None) -> str:
    if not timestamp:
        return "unknown"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(timestamp)))
    except (ValueError, OverflowError, OSError):
        return "unknown"
