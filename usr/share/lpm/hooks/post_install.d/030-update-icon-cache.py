#!/usr/bin/env python3
import os
import shutil
import subprocess
from pathlib import Path


def main():
    tool = shutil.which("gtk-update-icon-cache")
    if not tool:
        return
    root = Path(os.environ.get("LPM_ROOT", "/"))
    icons_root = root / "usr/share/icons"
    if not icons_root.is_dir():
        return
    for d in icons_root.iterdir():
        if not d.is_dir():
            continue
        if not (d / "index.theme").exists():
            continue
        subprocess.run([tool, str(d)], check=False)


if __name__ == "__main__":
    main()
