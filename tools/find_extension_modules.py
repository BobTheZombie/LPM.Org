#!/usr/bin/env python3
from __future__ import annotations

import modulefinder
import sys
from importlib.machinery import EXTENSION_SUFFIXES
from pathlib import Path
from typing import Iterable


def iter_extension_modules(finder: modulefinder.ModuleFinder) -> Iterable[str]:
    suffixes = tuple(EXTENSION_SUFFIXES)
    for name, module in finder.modules.items():
        module_file = getattr(module, "__file__", None)
        if not module_file:
            continue
        if module_file.endswith(suffixes):
            yield name


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    entry = root / "lpm.py"

    search_path = [str(root / "src"), str(root)] + sys.path
    finder = modulefinder.ModuleFinder(path=search_path)

    try:
        finder.run_script(str(entry))
    except ImportError:
        # Missing optional modules (like optional compression backends) shouldn't
        # cause the helper script to abort. Nuitka will warn about unresolved
        # imports during the real build where the environment is complete.
        pass

    modules = sorted(set(iter_extension_modules(finder)))
    output = " ".join(modules)
    if output:
        sys.stdout.write(output + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
