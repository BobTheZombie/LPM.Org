"""Flatpak/Flathub CLI integration for LPM Linux.

Flatpak transactions are delegated to Flatpak itself, not LPM's SAT solver or
PackageKit's LPM backend. This preserves OSTree/GPG verification and sandbox
permissions while allowing native packages and Flatpaks to coexist.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess

FLATHUB_REPO = "https://dl.flathub.org/repo/flathub.flatpakrepo"
# A Flatpak app/ref ID, not a shell expression, filesystem path or remote URL.
_APP_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+){1,}(?:/[A-Za-z0-9_]+/[A-Za-z0-9_.-]+)?$")


def _scope_option(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--user", dest="flatpak_scope", action="store_const", const="user",
                       help="use the current user's Flatpak installation")
    group.add_argument("--system", dest="flatpak_scope", action="store_const", const="system",
                       help="use the system Flatpak installation (default)")
    parser.set_defaults(flatpak_scope="system")


def add_flatpak_parser(subparsers) -> None:
    """Register an independent Flatpak namespace with the LPM argument parser."""
    parser = subparsers.add_parser(
        "flatpak", help="Manage Flathub applications (delegates to Flatpak)"
    )
    commands = parser.add_subparsers(dest="flatpak_action", required=True)

    p = commands.add_parser("setup", help="add official Flathub remote, preserving existing remotes")
    _scope_option(p)

    p = commands.add_parser("search", help="search for Flatpak applications")
    p.add_argument("terms", nargs="+", help="application search terms")

    p = commands.add_parser("install", help="install applications from Flathub")
    _scope_option(p)
    p.add_argument("-y", "--yes", action="store_true", help="accept Flatpak's confirmation prompts")
    p.add_argument("apps", nargs="+", help="Flatpak app IDs, e.g. org.mozilla.firefox")

    p = commands.add_parser("remove", help="uninstall Flatpak applications")
    _scope_option(p)
    p.add_argument("-y", "--yes", action="store_true")
    p.add_argument("apps", nargs="+")

    p = commands.add_parser("update", help="update Flatpak applications and runtimes")
    _scope_option(p)
    p.add_argument("-y", "--yes", action="store_true")
    p.add_argument("apps", nargs="*")

    p = commands.add_parser("list", help="list installed Flatpak applications")
    _scope_option(p)

    p = commands.add_parser("remotes", help="list configured Flatpak repositories")
    _scope_option(p)

    p = commands.add_parser("info", help="show Flatpak application information")
    _scope_option(p)
    p.add_argument("app")

    p = commands.add_parser("run", help="launch an installed Flatpak application")
    _scope_option(p)
    p.add_argument("app")

    parser.set_defaults(func=cmd_flatpak)


def _validate_apps(apps) -> None:
    for app in apps:
        if not _APP_ID.fullmatch(app):
            raise SystemExit(f"Invalid Flatpak application ID: {app!r}")



def list_installed_flatpaks() -> list[tuple[str, str, str, str, str]] | None:
    """Read apps from every Flatpak installation (system and current user).

    This is intentionally read-only. Installation and remote ownership remain
    with Flatpak, and the native LPM SQLite database is never modified.
    Returns None when the Flatpak executable is absent.
    """
    executable = shutil.which("flatpak")
    if executable is None:
        return None

    try:
        completed = subprocess.run(
            [executable, "list", "--app",
             "--columns=application:full,version:full,arch:full,origin:full,installation:full"],
            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not list installed Flatpaks: {exc}") from exc

    if completed.returncode:
        detail = (completed.stderr or "").strip().splitlines()
        message = detail[-1] if detail else f"exit status {completed.returncode}"
        raise RuntimeError(f"Could not list installed Flatpaks: {message}")

    applications = []
    for line in completed.stdout.splitlines():
        if not line.strip():
            continue
        columns = [field.strip() for field in line.split("\t")]
        # Flatpak may print a header depending on its output mode/version.
        if columns[0].lower() in ("application", "application id"):
            continue
        if len(columns) != 5 or not _APP_ID.fullmatch(columns[0]):
            raise RuntimeError("Unexpected Flatpak list output; refusing partial inventory")
        applications.append(tuple(columns))

    return sorted(applications, key=lambda app: (app[0].lower(), app[4], app[3]))

def cmd_flatpak(args) -> int:
    """Execute a constrained Flatpak command with no shell or LPM DB mutations."""
    executable = shutil.which("flatpak")
    if executable is None:
        raise SystemExit("Flatpak is not installed. Run: lpm install flatpak flatpak-flathub")

    operation = args.flatpak_action
    scope = "--user" if getattr(args, "flatpak_scope", "system") == "user" else "--system"
    yes = ["--assumeyes"] if getattr(args, "yes", False) else []
    if operation == "setup":
        argv = ["remote-add", scope, "--if-not-exists", "--from", "flathub", FLATHUB_REPO]
    elif operation == "search":
        argv = ["search", " ".join(args.terms)]
    elif operation == "install":
        _validate_apps(args.apps)
        argv = ["install", scope, *yes, "flathub", *args.apps]
    elif operation == "remove":
        _validate_apps(args.apps)
        argv = ["uninstall", scope, *yes, *args.apps]
    elif operation == "update":
        _validate_apps(args.apps)
        argv = ["update", scope, *yes, *args.apps]
    elif operation == "list":
        argv = ["list", scope, "--app"]
    elif operation == "remotes":
        argv = ["remote-list", scope]
    elif operation == "info":
        _validate_apps([args.app])
        argv = ["info", scope, args.app]
    elif operation == "run":
        _validate_apps([args.app])
        argv = ["run", scope, args.app]
    else:
        raise SystemExit(f"Unsupported Flatpak action: {operation}")

    try:
        completed = subprocess.run([executable, *argv], check=False)
    except OSError as exc:
        raise SystemExit(f"Could not run Flatpak: {exc}") from exc
    if completed.returncode:
        raise SystemExit(completed.returncode)
    return 0
