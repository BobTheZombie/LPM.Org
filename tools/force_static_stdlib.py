#!/usr/bin/env python3
"""Rewrite Modules/Setup.stdlib to build extensions into libpython.

This helper generates a ``Setup.local`` file that marks all built-in
extensions as ``*static*`` so they are folded into ``libpython`` when the
static toolchain is compiled. Nuitka's ``--static-libpython=yes`` option
expects the interpreter to provide these modules without relying on shared
objects living alongside the executable.

The script replaces the first ``*shared*`` (or ``*@MODULE_BUILDTYPE@*``
placeholder) directive in ``Setup.stdlib`` with ``*static*`` and writes the
result to ``Setup.local``. Any modules that configure disabled remain
commented out.
"""
from __future__ import annotations

import argparse
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Force libpython to include all built-in extension modules by "
            "generating a static Modules/Setup.local file."
        )
    )
    parser.add_argument(
        "source",
        type=Path,
        help="Path to Modules/Setup.stdlib produced by the CPython configure step.",
    )
    parser.add_argument(
        "output",
        type=Path,
        nargs="?",
        help="Destination path for the generated Modules/Setup.local file. Defaults to the Setup.local file alongside the source.",
    )
    return parser.parse_args()


def make_static(content: str) -> str:
    lines = content.splitlines()
    replaced = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if "MODULE_BUILDTYPE" in stripped and stripped.startswith("*"):
            lines[index] = "*static*"
            replaced = True
            break
        if stripped in {"*shared*", "*static*"} and not replaced:
            lines[index] = "*static*"
            replaced = True
            break
    if not replaced:
        raise RuntimeError(
            "Unable to locate MODULE_BUILDTYPE directive in Setup.stdlib; "
            "cannot force static modules."
        )
    # Preserve a trailing newline when the input had one to avoid needless diffs.
    ending = "\n" if content.endswith("\n") else ""
    return "\n".join(lines) + ending


def main() -> None:
    args = parse_args()
    source = args.source
    output = args.output or source.with_name("Setup.local")

    if not source.is_file():
        raise SystemExit(f"Setup template '{source}' does not exist")

    content = source.read_text()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(make_static(content))


if __name__ == "__main__":
    main()
