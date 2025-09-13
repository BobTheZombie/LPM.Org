#!/usr/bin/env python3
import os
import shutil
import subprocess
from pathlib import Path


def main():
    tool = shutil.which("update-desktop-database")
    if not tool:
        return
    root = Path(os.environ.get("LPM_ROOT", "/"))
    apps = root / "usr/share/applications"
    if not apps.is_dir():
        return
    subprocess.run([tool, str(apps)], check=False)


if __name__ == "__main__":
    main()
