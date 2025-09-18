from __future__ import annotations

"""Utility for retrieving the LPM version string.

The value is sourced from ``lpm.__version__`` which itself honours the
``LPM_VERSION`` environment variable (falling back to a static default).
When the constant is unavailable the script falls back to ``git describe`` to
mirror the previous behaviour.
"""

import pathlib
import re
import subprocess


def main() -> None:
    root = pathlib.Path(__file__).resolve().parents[1]
    version: str | None = None
    lpm_path = root / "lpm.py"
    if lpm_path.exists():
        match = re.search(r"__version__\s*=\s*['\"]([^'\"]+)['\"]", lpm_path.read_text(encoding="utf-8"))
        if match:
            version = match.group(1)
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
