"""Root privilege helpers with automatic escalation.

This module centralises privilege checks and, when allowed, attempts to
re-execute the current process via ``sudo`` to acquire the privileges required
by package management operations. Users can opt out of escalation either by
passing ``--no-escalate`` (which calls :func:`set_escalation_disabled`) or by
setting the ``LPM_NO_ESCALATE`` environment variable.
"""

from __future__ import annotations

import os
import shlex
import sys
from typing import Iterable

_escalation_disabled = False
_prompt_context: str | None = None

__all__ = [
    "RootPrivilegesRequired",
    "ensure_root_or_escalate",
    "format_command_for_hint",
    "require_root",
    "escalation_disabled",
    "is_root",
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


def is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    if not callable(geteuid):
        return False
    try:
        return geteuid() == 0
    except OSError:
        return False


# Backwards-compatible alias
_is_root = is_root


def set_escalation_disabled(value: bool) -> None:
    """Enable or disable automatic privilege escalation."""

    global _escalation_disabled
    _escalation_disabled = bool(value)


def escalation_disabled() -> bool:
    """Return ``True`` when escalation has been disabled."""

    return _escalation_disabled or bool(os.environ.get("LPM_NO_ESCALATE"))


def set_prompt_context(context: str) -> None:
    """Record a context string for external prompt handlers."""

    global _prompt_context
    _prompt_context = context


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


def ensure_root_or_escalate(intent: str | None = None) -> None:
    """Ensure the current process has root privileges.

    When not running as root, this function re-executes the current program via
    ``sudo`` with the original command-line arguments so that privileged
    operations can proceed. Escalation can be disabled with the ``--no-escalate``
    flag (which calls :func:`set_escalation_disabled`) or by setting the
    ``LPM_NO_ESCALATE`` environment variable.
    """

    if _escalation_disabled or os.environ.get("LPM_NO_ESCALATE"):
        require_root(intent)
        return

    if _is_root():
        return

    prog = sys.argv[0]
    args = sys.argv[1:]

    try:
        os.execvp("sudo", ["sudo", prog] + args)
    except Exception:
        # Fall back to the standard root check to raise a helpful error
        require_root(intent)

