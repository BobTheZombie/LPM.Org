from __future__ import annotations

import importlib
import os
import shlex
import shutil
import sys
from pathlib import Path
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


_FALLBACK_SCRIPT = """
import importlib
import sys

argv = sys.argv[1:]

if argv and argv[0] == 'installpkg':
    module = importlib.import_module('lpm.installpkg')
    main = getattr(module, 'main', None)
    if main is None:
        raise SystemExit('lpm.installpkg.main is unavailable; cannot re-exec with privileges')
    sys.exit(main(argv[1:]))

module = importlib.import_module('lpm')
main = getattr(module, 'main', None)
if main is None:
    raise SystemExit('lpm.main is unavailable; cannot re-exec with privileges')

sys.exit(main(argv))
""".strip()


def _maybe_prepend_pythonpath(module_name: str = "lpm") -> None:
    """Ensure *module_name* can be imported after privilege escalation.

    When running from a onefile bundle the temporary extraction directory is
    used as the import root for :mod:`lpm`.  Once the current process exits that
    directory may be removed, so re-executing via ``sudo python -c`` would fail
    to import :mod:`lpm`.  To make the module importable we explicitly prepend
    its import root to ``PYTHONPATH`` so the privileged process can resolve it
    before the temporary directory disappears.
    """

    try:
        module = importlib.import_module(module_name)
    except Exception:  # pragma: no cover - defensive fallback
        return

    candidates: List[Path] = []

    spec = getattr(module, "__spec__", None)
    search_locations = getattr(spec, "submodule_search_locations", None)
    if search_locations:
        for location in search_locations:
            try:
                path = Path(location)
            except (TypeError, ValueError):
                continue
            parent = path.parent if path.name == module_name.split(".")[-1] else path
            candidates.append(parent)

    module_file = getattr(module, "__file__", None)
    if module_file:
        try:
            resolved = Path(module_file).resolve()
        except (OSError, RuntimeError, ValueError):
            pass
        else:
            parent = resolved.parent
            if parent.name == module_name.split(".")[-1]:
                parent = parent.parent
            candidates.append(parent)

    if not candidates:
        return

    existing_env = os.environ.get("PYTHONPATH")
    existing_parts = [part for part in (existing_env or "").split(os.pathsep) if part]
    seen = set(existing_parts)

    new_parts: List[str] = []
    for candidate in candidates:
        try:
            candidate_str = os.fspath(candidate)
        except TypeError:
            continue
        if not candidate_str:
            continue
        if candidate_str in seen:
            continue
        seen.add(candidate_str)
        new_parts.append(candidate_str)

    if not new_parts:
        return

    if existing_parts:
        os.environ["PYTHONPATH"] = os.pathsep.join(new_parts + existing_parts)
    else:
        os.environ["PYTHONPATH"] = os.pathsep.join(new_parts)


def _normalize_argv_for_privileged_exec(argv: List[str]) -> List[str]:
    """Return an argv suitable for re-execing under elevated privileges."""

    if not argv:
        return argv

    def _needs_module_fallback(path: Path) -> bool:
        return path.is_absolute() and any(part.startswith("onefile_") for part in path.parts)

    potential_paths = []

    try:
        potential_paths.append(Path(argv[0]))
    except (TypeError, ValueError):
        pass

    exec_candidate = getattr(sys, "executable", None)
    if exec_candidate:
        try:
            potential_paths.append(Path(exec_candidate))
        except (TypeError, ValueError):
            pass

    if any(_needs_module_fallback(path) for path in potential_paths if path is not None):
        for candidate in ("python3", "python", "pypy3", "pypy"):
            resolved = shutil.which(candidate)
            if resolved and os.access(resolved, os.X_OK):
                _maybe_prepend_pythonpath()
                return [resolved, "-c", _FALLBACK_SCRIPT, *argv[1:]]

    return argv


def _exec_sudo(argv: Iterable[str]) -> None:
    _log_escalation("[escalate] using sudo")
    normalized = _normalize_argv_for_privileged_exec(list(argv))
    os.execvp("sudo", ["sudo", "-E", *normalized])


def _exec_pkexec(argv: Iterable[str]) -> None:
    _log_escalation("[escalate] using pkexec")
    normalized = _normalize_argv_for_privileged_exec(list(argv))
    env: Dict[str, str] = {
        key: value
        for key, value in os.environ.items()
        if key in _PKEXEC_ENV_ALLOWLIST
    }
    os.execvpe("pkexec", ["pkexec", *normalized], env)


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
