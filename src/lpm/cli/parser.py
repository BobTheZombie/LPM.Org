"""Argument parser construction for the CLI."""

from __future__ import annotations

import argparse

from . import as_root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lpm")
    parser.add_argument(as_root.AS_ROOT_FLAG, action="store_true", dest="as_root", help=argparse.SUPPRESS)

    sub = parser.add_subparsers(dest="command")

    install = sub.add_parser("install", help="Install one or more packages")
    install.add_argument("packages", nargs="*", metavar="PKG")
    install.add_argument("--plan", dest="plan_path", help=argparse.SUPPRESS)

    return parser


__all__ = ["build_parser"]

