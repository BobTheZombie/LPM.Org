"""Runtime context shared across CLI commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from . import as_root
from .utils import is_root


@dataclass(slots=True)
class CLIContext:
    """Snapshot of the CLI invocation details."""

    prog: str
    raw_args: Sequence[str]
    triggered: bool

    @property
    def running_as_root(self) -> bool:
        return is_root()

    @property
    def escalation_triggered(self) -> bool:
        return self.triggered or as_root.triggered()


__all__ = ["CLIContext"]

