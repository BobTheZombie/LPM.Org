"""Compatibility shims for privilege handling.

The previous implementation provided interactive prompts and multiple
escalation backends.  The helpers in this module now provide a lightweight
confirmation flow that re-executes the running command via ``sudo`` when root
privileges are required but not currently available.
"""

from __future__ import annotations

import getpass
import os
import shlex
import shutil
import subprocess
import sys
from typing import Iterable, Sequence

from .cli import as_root

__all__ = [
    "RootPrivilegesRequired",
    "ensure_root_or_escalate",
    "format_command_for_hint",
    "require_root",
    "set_escalation_disabled",
    "set_prompt_context",
]


class RootPrivilegesRequired(PermissionError):
    """Raised when an operation needs root privileges but none are present."""

    def __init__(self, intent: str | None = None):
        message = "Root privileges are required"
        if intent:
            message += f" to {intent}"
        message += f". Re-run with 'sudo {format_command_for_hint()} {as_root.AS_ROOT_FLAG}'."
        super().__init__(message)
        self.intent = intent


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    if not callable(geteuid):
        return False
    try:
        return geteuid() == 0
    except OSError:
        return False


def set_escalation_disabled(_value: bool) -> None:
    """Retained for compatibility; the new CLI always opts-in explicitly."""


def set_prompt_context(_context: str) -> None:
    """Retained for compatibility; prompts are no longer interactive."""


def format_command_for_hint(argv: Iterable[str] | None = None) -> str:
    argv = list(argv if argv is not None else sys.argv)
    if not argv:
        executable = getattr(sys, "executable", None) or "python3"
        argv = [executable, "-m", "lpm"]
    return shlex.join(argv)


def require_root(intent: str | None = None) -> None:
    if _is_root():
        return
    raise RootPrivilegesRequired(intent)


def _prompt_confirmation(intent: str) -> bool:
    stream = getattr(sys, "stdin", None)
    is_tty = False
    if stream is not None:
        try:
            is_tty = stream.isatty()
        except Exception:
            is_tty = False
    if not is_tty:
        return False

    try:
        response = input(
            f"Root privileges are required to {intent}. Continue with sudo? [y/N]: "
        )
    except (EOFError, OSError):
        return False
    return response.strip().lower() in {"y", "yes"}


def _prompt_password() -> str | None:
    try:
        return getpass.getpass("sudo password: ")
    except (EOFError, KeyboardInterrupt):
        return None


def _sudo_command(argv: Sequence[str]) -> list[str] | None:
    sudo = shutil.which("sudo")
    if not sudo:
        return None
    return [sudo, "-S", "-E", *argv]


def ensure_root_or_escalate(intent: str) -> None:
    """Ensure the current process has root privileges, escalating via sudo if needed."""

    if _is_root():
        return

    if not _prompt_confirmation(intent):
        raise RootPrivilegesRequired(intent)

    password = _prompt_password()
    if not password:
        raise RootPrivilegesRequired(intent)

    command = _sudo_command(sys.argv)
    if command is None:
        raise RootPrivilegesRequired(intent)

    result = subprocess.run(
        command,
        input=f"{password}\n".encode(),
        check=False,
    )
    if result.returncode != 0:
        raise RootPrivilegesRequired(intent)

    sys.exit(result.returncode)

