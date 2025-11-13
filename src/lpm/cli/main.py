"""CLI entry point for :mod:`lpm`."""

from __future__ import annotations

import sys
from typing import Iterable

from . import as_root
from .commands.install import InstallCommand, InstallOptions
from .context import CLIContext
from .parser import build_parser


def main(argv: Iterable[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    parser = build_parser()
    args = parser.parse_args(list(argv))

    context = CLIContext(parser.prog, list(argv), args.as_root or as_root.triggered())

    command = args.command
    if command == "install":
        options = InstallOptions(packages=list(args.packages), plan_path=args.plan_path)
        handler = InstallCommand(context)
        return handler.execute(options)

    parser.print_help()
    return 0


__all__ = ["main"]

