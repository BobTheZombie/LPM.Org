from __future__ import annotations

"""Utility for retrieving the LPM version string.

The value is sourced from ``lpm.__version__`` which itself honours the
``LPM_VERSION`` environment variable (falling back to a static default).
When the constant is unavailable the script falls back to ``git describe`` to
mirror the previous behaviour.
"""

import pathlib
import subprocess
import sys


def main() -> None:
    root = pathlib.Path(__file__).resolve().parents[1]
    version: str | None = None
    src_root = root / "src"
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))
    from lpm.app import get_runtime_metadata

    version = get_runtime_metadata().get("version")
    if version is None:
        try:
            version = subprocess.check_output([
                "git",
                "describe",
                "--tags",
                "--always",
            ], cwd=root).decode().strip()
        except Exception:
            version = "0.0.0"
    print(version)


if __name__ == "__main__":
    main()
