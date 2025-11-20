"""Argument parser construction for the CLI."""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lpm")

    sub = parser.add_subparsers(dest="command")

    install = sub.add_parser("install", help="Install one or more packages")
    install.add_argument("packages", nargs="*", metavar="PKG")
    install.add_argument("--plan", dest="plan_path", help=argparse.SUPPRESS)

    return parser


__all__ = ["build_parser"]

