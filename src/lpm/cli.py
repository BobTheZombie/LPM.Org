from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from typing import Any

ROOT_HELPER_FLAG = "--__lpm-root-install"


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``lpm`` command line interface."""

    if argv is None:
        argv = sys.argv[1:]

    if argv and argv[0] == ROOT_HELPER_FLAG:
        return _root_install_main(argv[1:])

    parser = argparse.ArgumentParser(prog="lpm")
    subparsers = parser.add_subparsers(dest="command")

    install_parser = subparsers.add_parser(
        "install", help="Install one or more packages"
    )
    install_parser.add_argument("packages", nargs="+", metavar="PKG")

    args = parser.parse_args(argv)

    if args.command == "install":
        return _user_install(list(args.packages))

    parser.print_help()
    return 0


def build_install_plan(pkgs: list[str]) -> dict[str, Any]:
    """Produce an installation plan for *pkgs*.

    This is a minimal placeholder that records the package identifiers.  The
    resulting dictionary is passed verbatim to
    :func:`lpm.installpkg.apply_install_plan` once the process is running with
    elevated privileges.
    """

    return {"packages": list(pkgs)}


def _user_install(pkgs: list[str]) -> int:
    plan = build_install_plan(pkgs)

    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as temp:
        json.dump(plan, temp)
        plan_path = temp.name

    try:
        return _run_root_helper(plan_path)
    finally:
        try:
            os.unlink(plan_path)
        except FileNotFoundError:
            pass


def _run_root_helper(plan_path: str) -> int:
    if _is_root():
        return _root_install_main([plan_path])

    package = __package__ or "src.lpm"
    module = f"{package}.cli"
    cmd = ["sudo", sys.executable, "-m", module, ROOT_HELPER_FLAG, plan_path]
    result = subprocess.run(cmd, check=False)
    return result.returncode


def _root_install_main(argv: list[str]) -> int:
    if not _is_root():
        print("lpm: root privileges are required to apply the install plan", file=sys.stderr)
        return 1

    if not argv:
        print("usage: lpm --__lpm-root-install <plan.json>", file=sys.stderr)
        return 2

    plan_path = argv[0]

    try:
        with open(plan_path, "r", encoding="utf-8") as fh:
            plan = json.load(fh)
    except FileNotFoundError:
        print(f"lpm: install plan not found: {plan_path}", file=sys.stderr)
        return 3
    except json.JSONDecodeError as exc:
        print(f"lpm: failed to parse install plan: {exc}", file=sys.stderr)
        return 3

    try:
        from .installpkg import apply_install_plan
    except Exception as exc:  # pragma: no cover - defensive
        print(f"lpm: unable to import privileged installer: {exc}", file=sys.stderr)
        return 4

    try:
        return int(apply_install_plan(plan))
    except Exception as exc:
        print(f"lpm: privileged install failed: {exc}", file=sys.stderr)
        return 5


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid):
        try:
            return geteuid() == 0
        except OSError:
            return False
    return False


if __name__ == "__main__":
    raise SystemExit(main())
