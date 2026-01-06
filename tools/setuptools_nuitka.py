"""Setuptools commands that integrate Nuitka compilation into project builds."""

from __future__ import annotations

import importlib.util
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Sequence

from setuptools import Command
from setuptools.command.build_py import build_py


class BuildNuitka(Command):
    """Compile the CLI and optional UI launchers using Nuitka."""

    description = "compile LPM entry points with Nuitka"
    user_options: list[tuple[str, str, str]] = []

    def initialize_options(self) -> None:  # pragma: no cover - distutils API
        self.skip = False

    def finalize_options(self) -> None:  # pragma: no cover - distutils API
        pass

    def run(self) -> None:  # pragma: no cover - behaviour validated via wheel contents
        if os.environ.get("LPM_SKIP_NUITKA") == "1":
            self.announce("LPM_SKIP_NUITKA=1 set, skipping Nuitka compilation", level=2)
            return

        build_py_cmd = self.get_finalized_command("build_py")
        build_lib = Path(getattr(build_py_cmd, "build_lib"))
        output_dir = build_lib / "lpm" / "bin"
        output_dir.mkdir(parents=True, exist_ok=True)

        project_root = Path(__file__).resolve().parents[1]
        base_flags = self._base_flags(output_dir)
        extra_flags = shlex.split(os.environ.get("LPM_NUITKA_FLAGS", ""))
        self._build_script(
            script_name="lpm",
            script_path=project_root / "lpm.py",
            flags=[*base_flags, *extra_flags],
        )

        if self._should_build_ui():
            ui_flags = shlex.split(
                os.environ.get("LPM_NUITKA_UI_FLAGS", "--enable-plugin=pyside6")
            )
            self._build_script(
                script_name="lpm-ui",
                script_path=project_root / "lpm_ui.py",
                flags=[*base_flags, *extra_flags, *ui_flags],
            )
        else:
            self.announce("Skipping GUI build (PySide6 not available)", level=2)

    def _base_flags(self, output_dir: Path) -> list[str]:
        cpu_count = os.cpu_count() or 1
        return [
            f"--output-dir={output_dir}",
            "--onefile",
            "--follow-imports",
            "--include-package=src",
            "--include-package=packaging",
            "--lto=yes",
            f"--jobs={cpu_count}",
            "--python-flag=-O",
        ]

    def _should_build_ui(self) -> bool:
        if os.environ.get("LPM_NUITKA_SKIP_UI") == "1":
            return False
        return os.environ.get("LPM_NUITKA_FORCE_UI") == "1" or bool(
            importlib.util.find_spec("PySide6")
        )

    def _nuitka_invocation(self, flags: Iterable[str], script_path: Path) -> Sequence[str]:
        runner = shlex.split(os.environ.get("LPM_NUITKA_BIN", ""))
        if not runner:
            runner = [sys.executable, "-m", "nuitka"]
        return [*runner, *flags, str(script_path)]

    def _build_script(self, script_name: str, script_path: Path | None, flags: list[str]) -> None:
        if script_path is None or not script_path.exists():
            raise FileNotFoundError(f"Unable to locate launcher: {script_name}")
        command = self._nuitka_invocation(flags, script_path)
        self.announce(f"Building {script_name} with Nuitka", level=2)
        subprocess.check_call(command)


class BuildPyWithNuitka(build_py):
    """Run the standard build then compile entry points with Nuitka."""

    def run(self) -> None:  # pragma: no cover - distutils API
        super().run()
        self.run_command("build_nuitka")
