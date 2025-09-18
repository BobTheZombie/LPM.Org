from __future__ import annotations

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
