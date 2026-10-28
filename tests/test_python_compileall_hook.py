"""Regression tests for post-transaction Python bytecode hook.

PySide6 wheels contain Jinja template files ending in .tmpl.py; these must
never be compiled as Python when installing the binary package.
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


HOOK = Path(__file__).resolve().parents[1] / "usr/libexec/lpm/hooks/python-compileall"


def _run(root: Path, *targets: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["LPM_ROOT"] = str(root)
    env.pop("LPM_TARGETS", None)
    env.pop("LPM_TARGETS_FILE", None)
    return subprocess.run(
        [sys.executable, str(HOOK), *targets],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_pyside_jinja_templates_not_compiled_as_python(tmp_path: Path) -> None:
    pkg = tmp_path / "usr/lib/python3.14/site-packages/PySide6"
    pkg.mkdir(parents=True)
    regular = pkg / "__init__.py"
    regular.write_text("VERSION = 6\n", encoding="utf-8")
    android = pkg / "scripts/deploy_lib/android/recipes/PySide6"
    android.mkdir(parents=True)
    template = android / "__init__.tmpl.py"
    template.write_text("{% for module in qt_modules %}\n", encoding="utf-8")
    # Existing, unrelated broken sources must not block another install.
    unrelated = pkg / "unrelated_broken.py"
    unrelated.write_text("this is ! bad Python\n", encoding="utf-8")

    paths = [
        "/usr/lib/python3.14/site-packages/PySide6/__init__.py",
        "/usr/lib/python3.14/site-packages/PySide6/scripts/deploy_lib/android/recipes/PySide6/__init__.tmpl.py",
        "/usr/lib/python3.14/site-packages/PySide6/QtCore.pyi",
    ]
    result = _run(tmp_path, *paths)
    assert result.returncode == 0, result.stderr + result.stdout
    cache = pkg / "__pycache__"
    assert list(cache.glob("__init__*.pyc"))
    assert not list(android.rglob("*.pyc"))
    assert not list(cache.glob("unrelated_broken*.pyc"))


def test_real_python_syntax_errors_still_fail_hook(tmp_path: Path) -> None:
    pkg = tmp_path / "usr/lib/python3.14/site-packages/realpkg"
    pkg.mkdir(parents=True)
    bad = pkg / "__init__.py"
    bad.write_text("x = { broken\n", encoding="utf-8")
    result = _run(tmp_path, "/usr/lib/python3.14/site-packages/realpkg/__init__.py")
    assert result.returncode == 1
    assert "SyntaxError" in result.stdout + result.stderr


def test_manual_directory_mode_ignores_templates(tmp_path: Path) -> None:
    pkg = tmp_path / "usr/lib/python3.14/site-packages/realpkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    (pkg / "__init__.tmpl.py").write_text("{% for x in y %}\n", encoding="utf-8")
    result = _run(tmp_path, "/usr/lib/python3.14/site-packages/realpkg")
    assert result.returncode == 0, result.stderr + result.stdout
    assert list((pkg / "__pycache__").glob("__init__*.pyc"))
    assert not list((pkg / "__pycache__").glob("__init__.tmpl*.pyc"))
