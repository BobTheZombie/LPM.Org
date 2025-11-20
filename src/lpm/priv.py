"""Root privilege helpers.

The codebase now assumes callers run LPM with traditional ``sudo`` rather than
attempting automatic privilege escalation. These helpers simply enforce that
requirement and provide consistent error messaging.
"""

from __future__ import annotations

import os
import shlex
import sys
from typing import Iterable

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
        message += f". Re-run with 'sudo {format_command_for_hint()}'."
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


def format_command_for_hint(argv: Iterable[str] | None = None) -> str:
    args = list(argv if argv is not None else sys.argv)
    if not args:
        executable = getattr(sys, "executable", None) or "python3"
        args = [executable, "-m", "lpm"]
    return shlex.join(args)


def require_root(intent: str | None = None) -> None:
    if _is_root():
        return
    raise RootPrivilegesRequired(intent)


def ensure_root_or_escalate(intent: str) -> None:
    """Ensure the current process has root privileges."""

    require_root(intent)

