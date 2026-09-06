"""Stable state shared by public facades across deliberate module reloads."""

from __future__ import annotations

from types import ModuleType


facades: list[ModuleType] = []


def register(module: ModuleType) -> None:
    if module not in facades:
        facades.append(module)
