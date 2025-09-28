#!/usr/bin/env python3
import os
import shutil
import subprocess


def main():
    # Only run ldconfig when installing to the real root
    if os.environ.get("LPM_ROOT", "/") != "/":
        return
    tool = shutil.which("ldconfig")
    if not tool:
        return
    subprocess.run([tool], check=False)


if __name__ == "__main__":
    main()
