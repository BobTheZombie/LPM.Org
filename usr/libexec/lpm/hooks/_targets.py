#!/usr/bin/env python3
from __future__ import annotations

import os
from typing import Iterable, List


def _dedupe(items: Iterable[str]) -> List[str]:
    seen = set()
    order: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            order.append(item)
    return order


def collect_targets(argv: Iterable[str]) -> List[str]:
    """Return target paths from the environment and positional arguments."""

    values: List[str] = []
    env_targets = os.environ.get("LPM_TARGETS", "")
    if env_targets:
        for line in env_targets.splitlines():
            text = line.strip()
            if text:
                values.append(text)
    for arg in argv:
        text = str(arg).strip()
        if text:
            values.append(text)
    return _dedupe(values)

