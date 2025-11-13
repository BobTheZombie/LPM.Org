"""Compatibility shims for privilege handling.

The previous implementation provided interactive prompts and multiple
escalation backends.  The new CLI performs a single ``sudo`` re-exec when the
``--as-root`` trigger is present, so the helpers in this module simply validate
that the current process already has the necessary privileges.
"""

from __future__ import annotations

import os
import shlex
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


def ensure_root_or_escalate(intent: str) -> None:
    """Compatibility wrapper for legacy callers."""

    require_root(intent)

