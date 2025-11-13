"""Implementation of the ``install`` sub-command."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Sequence

from ...installpkg import apply_install_plan
from ...priv import require_root
from .. import as_root
from ..context import CLIContext
from ..planfile import InstallPlanFile
from ..planner import build_install_plan


@dataclass(slots=True)
class InstallOptions:
    packages: Sequence[str]
    plan_path: str | None


class InstallCommand:
    """Handle package installation requests."""

    def __init__(self, context: CLIContext) -> None:
        self._context = context

    def execute(self, options: InstallOptions) -> int:
        if options.plan_path:
            return self._apply_plan_from_path(options.plan_path)

        if not options.packages:
            print("lpm: at least one package must be specified", file=sys.stderr)
            return 2

        plan = build_install_plan(options.packages)
        if self._context.running_as_root or self._context.escalation_triggered:
            return self._apply_plan(plan)

        with InstallPlanFile(plan) as plan_file:
            if not plan_file.path:
                print("lpm: failed to create install plan", file=sys.stderr)
                return 3
            return as_root.invoke(["install", "--plan", plan_file.path])

    def _apply_plan(self, plan: dict) -> int:
        try:
            require_root("install packages")
        except PermissionError as exc:
            print(str(exc), file=sys.stderr)
            return 77

        try:
            return int(apply_install_plan(plan))
        except Exception as exc:  # pragma: no cover - defensive
            print(f"lpm: privileged install failed: {exc}", file=sys.stderr)
            return 5

    def _apply_plan_from_path(self, plan_path: str) -> int:
        try:
            with open(plan_path, "r", encoding="utf-8") as fh:
                plan = json.load(fh)
        except FileNotFoundError:
            print(f"lpm: install plan not found: {plan_path}", file=sys.stderr)
            return 3
        except json.JSONDecodeError as exc:
            print(f"lpm: failed to parse install plan: {exc}", file=sys.stderr)
            return 3
        finally:
            try:
                os.unlink(plan_path)
            except FileNotFoundError:
                pass

        return self._apply_plan(plan)


__all__ = ["InstallCommand", "InstallOptions"]

