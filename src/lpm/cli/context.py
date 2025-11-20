"""Runtime context shared across CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .utils import is_root


@dataclass(slots=True)
class CLIContext:
    """Snapshot of the CLI invocation details."""

    prog: str
    raw_args: Sequence[str]

    @property
    def running_as_root(self) -> bool:
        return is_root()


__all__ = ["CLIContext"]

