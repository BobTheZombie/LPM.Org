"""Privilege escalation helpers for the CLI."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Mapping, MutableMapping, Sequence

AS_ROOT_ENV = "LPM_AS_ROOT"
AS_ROOT_FLAG = "--as-root"


def _normalise_env(env: Mapping[str, str] | None = None) -> MutableMapping[str, str]:
    data: MutableMapping[str, str] = dict(os.environ)
    if env:
        data.update(env)
    data[AS_ROOT_ENV] = "1"
    return data


def _sudo_command(argv: Sequence[str]) -> list[str] | None:
    sudo = shutil.which("sudo")
    if not sudo:
        return None
    executable = getattr(sys, "executable", None) or "python3"
    return [sudo, "-E", executable, "-m", "lpm", AS_ROOT_FLAG, *argv]


def invoke(argv: Sequence[str], *, env: Mapping[str, str] | None = None) -> int:
    """Execute *argv* with root privileges using ``sudo``.

    The environment is tagged with :data:`AS_ROOT_ENV` so the re-executed
    process can recognise that the elevation already occurred.
    """

    command = _sudo_command(argv)
    if command is None:
        print("lpm: unable to locate 'sudo' for privilege escalation", file=sys.stderr)
        return 1
    result = subprocess.run(command, check=False, env=_normalise_env(env))
    return result.returncode


def triggered(env: Mapping[str, str] | None = None) -> bool:
    """Return ``True`` if the current environment indicates root mode."""

    env = env or os.environ
    return env.get(AS_ROOT_ENV) == "1"


__all__ = ["AS_ROOT_ENV", "AS_ROOT_FLAG", "invoke", "triggered"]

