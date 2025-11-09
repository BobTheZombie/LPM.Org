from __future__ import annotations

import os
import shlex
import shutil
import sys
from typing import Dict, Iterable, List, Tuple

__all__ = [
    "ensure_root_or_escalate",
    "format_command_for_hint",
    "set_escalation_disabled",
    "set_prompt_context",
]


_PKEXEC_ENV_ALLOWLIST: Tuple[str, ...] = (
    "LANG",
    "LC_ALL",
    "LC_MESSAGES",
    "TERM",
    "COLORTERM",
    "PATH",
    "DISPLAY",
    "XAUTHORITY",
    "DBUS_SESSION_BUS_ADDRESS",
)

_AUTO_ESCALATION_DISABLED = False
_PROMPT_CONTEXT = "default"


def set_escalation_disabled(value: bool) -> None:
    """Globally toggle automatic privilege escalation attempts."""

    global _AUTO_ESCALATION_DISABLED
    _AUTO_ESCALATION_DISABLED = bool(value)


def set_prompt_context(context: str) -> None:
    """Adjust how :func:`ensure_root_or_escalate` presents prompts."""

    global _PROMPT_CONTEXT
    _PROMPT_CONTEXT = context


def format_command_for_hint() -> str:
    """Return the current command line formatted for shell display."""

    argv = list(sys.argv)
    if not argv:
        executable = getattr(sys, "executable", None) or "python3"
        argv = [executable, "-m", "lpm"]
    return shlex.join(argv)


def _log_escalation(msg: str) -> None:
    print(msg, file=sys.stderr)


def _hint_and_exit() -> None:
    cmd = format_command_for_hint()
    print(f"[HINT] Try: sudo {cmd}", file=sys.stderr)
    raise SystemExit(77)


def _exec_sudo(argv: Iterable[str]) -> None:
    _log_escalation("[escalate] using sudo")
    os.execvp("sudo", ["sudo", "-E", *argv])


def _exec_pkexec(argv: Iterable[str]) -> None:
    _log_escalation("[escalate] using pkexec")
    env: Dict[str, str] = {
        key: value
        for key, value in os.environ.items()
        if key in _PKEXEC_ENV_ALLOWLIST
    }
    os.execvpe("pkexec", ["pkexec", *argv], env)


def ensure_root_or_escalate(intent: str) -> None:
    """Ensure the current process has root privileges or re-exec elevated."""

    if os.geteuid() == 0:
        return

    if _AUTO_ESCALATION_DISABLED:
        _hint_and_exit()

    argv: List[str] = list(sys.argv) or [getattr(sys, "executable", "python3"), "-m", "lpm"]
    stdin = getattr(sys, "stdin", None)
    is_tty = bool(getattr(stdin, "isatty", lambda: False)())
    sudo_path = shutil.which("sudo")
    pkexec_path = shutil.which("pkexec")

    context = _PROMPT_CONTEXT
    set_prompt_context("default")

    if not is_tty:
        if pkexec_path:
            try:
                _exec_pkexec(argv)
            except OSError:
                pass
        _hint_and_exit()

    if context != "permission-error":
        print(f"Root privileges are required to {intent}.", file=sys.stderr)
        print("", file=sys.stderr)

    options: List[Tuple[str, str]] = []
    if sudo_path:
        options.append(("y", "yes via sudo (terminal)"))
    if pkexec_path:
        options.append(("p", "pkexec (graphical)"))
    options.append(("n", "no, abort"))

    if len(options) == 1:
        _hint_and_exit()

    print("➤ Re-run with elevated privileges now?", file=sys.stderr)
    for key, label in options:
        print(f"  [{key.upper()}] {label}", file=sys.stderr)
    print("", file=sys.stderr)
    print("Tip: you can run this explicitly:", file=sys.stderr)
    print(f"  sudo {format_command_for_hint()}", file=sys.stderr)

    choice_default = "y" if sudo_path else ("p" if pkexec_path else "n")

    while True:
        try:
            response = input("Selection: ").strip().lower()
        except EOFError:
            response = "n"
        if not response:
            response = choice_default
        if response in {"y", "yes"} and sudo_path:
            try:
                _exec_sudo(argv)
            except OSError:
                break
            return
        if response in {"p", "pkexec"} and pkexec_path:
            try:
                _exec_pkexec(argv)
            except OSError:
                break
            return
        if response in {"n", "no"}:
            _hint_and_exit()
        print("Please choose a valid option (Y, P, or N).", file=sys.stderr)

    _hint_and_exit()
