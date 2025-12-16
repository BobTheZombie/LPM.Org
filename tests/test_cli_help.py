from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_module_help_exposes_full_cli():
    project_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "lpm", "--help"],
        cwd=project_root,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0

    stdout = result.stdout
    expected_commands = [
        "buildpkg",
        "list",
        "install",
        "remove",
        "upgrade",
        "search",
        "info",
    ]
    for command in expected_commands:
        assert command in stdout, f"missing '{command}' in help output:\n{stdout}"
