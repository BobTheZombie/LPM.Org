"""Helpers for constructing installation plans."""

from __future__ import annotations

from typing import Any, Iterable


def build_install_plan(packages: Iterable[str]) -> dict[str, Any]:
    """Return the plan dictionary that will be executed as ``root``.

    For now the plan simply records the package identifiers selected by the
    unprivileged front-end.  The privileged helper consumes the dictionary and
    performs the heavy lifting.
    """

    return {"packages": list(packages)}


__all__ = ["build_install_plan"]

