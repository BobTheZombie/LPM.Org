#!/usr/bin/env python3
"""Enumerate Python-based hook scripts for Nuitka compilation."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Iterable


def _iter_python_hook_scripts(base: Path) -> Iterable[Path]:
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix == ".py":
            yield path
            continue
        try:
            with path.open("rb") as handle:
                first_line = handle.readline()
        except OSError:
            continue
        if first_line.startswith(b"#!") and b"python" in first_line.lower():
            yield path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        nargs="?",
        default="usr/share/lpm/hooks",
        help="Root directory to scan for hook scripts",
    )
    args = parser.parse_args()
    base = Path(args.root)
    if not base.is_dir():
        return

    for path in _iter_python_hook_scripts(base):
        print(path)


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    main()
