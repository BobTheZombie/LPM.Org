"""Compatibility shims for privilege handling."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from typing import Iterable

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
        message += (
            f". Re-run with 'sudo {format_command_for_hint()} {as_root.AS_ROOT_FLAG}'."
        )
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
    """Retained for compatibility with legacy callers."""


def set_prompt_context(_context: str) -> None:
    """Retained for compatibility with legacy callers."""


def _filtered_argv(argv: Iterable[str]) -> list[str]:
    args = list(argv)
    try:
        flag_index = args.index(as_root.AS_ROOT_FLAG)
    except ValueError:
        return args
    # Remove the first occurrence so hints don't duplicate the flag.
    return args[:flag_index] + args[flag_index + 1 :]


def format_command_for_hint(argv: Iterable[str] | None = None) -> str:
    args = list(argv if argv is not None else sys.argv)
    if not args:
        executable = getattr(sys, "executable", None) or "python3"
        args = [executable, "-m", "lpm"]
    else:
        args = _filtered_argv(args)
    return shlex.join(args)


def require_root(intent: str | None = None) -> None:
    if _is_root() or as_root.triggered():
        return
    raise RootPrivilegesRequired(intent)


def _stdin_is_tty() -> bool:
    stream = getattr(sys, "stdin", None)
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def ensure_root_or_escalate(intent: str) -> None:
    """Ensure the current process has root privileges, escalating via sudo."""

    if _is_root() or as_root.triggered():
        return

    if not _stdin_is_tty():
        raise RootPrivilegesRequired(intent)

    if shutil.which("sudo") is None:
        raise RootPrivilegesRequired(intent)

    try:
        response = input(
            f"Root privileges are required to {intent}. Continue with sudo? [y/N]: "
        )
    except (EOFError, KeyboardInterrupt, OSError):
        raise RootPrivilegesRequired(intent)

    if response.strip().lower() not in {"y", "yes"}:
        raise RootPrivilegesRequired(intent)

    exit_code = as_root.invoke(sys.argv[1:])
    sys.exit(exit_code)

