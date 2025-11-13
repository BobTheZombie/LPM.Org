"""Temporary storage for install plans."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import AbstractContextManager
from typing import Any, Dict, Optional


class InstallPlanFile(AbstractContextManager["InstallPlanFile"]):
    """Persist an install plan to disk for consumption by the root helper."""

    def __init__(self, plan: Dict[str, Any]):
        self._plan = plan
        self.path: Optional[str] = None

    def __enter__(self) -> "InstallPlanFile":
        fd, path = tempfile.mkstemp(prefix="lpm-plan-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._plan, fh)
        self.path = path
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        if self.path:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass


__all__ = ["InstallPlanFile"]

