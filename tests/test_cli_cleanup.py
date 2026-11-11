"""Regression checks for the jhalfs-only bootstrap CLI cleanup."""
from __future__ import annotations

from lpm import app as lpm


REMOVED = (
    "bootstrap",
    "bootstrap-chroot",
    "installroot",
    "buildgen",
    "buildchroot",
)


def test_obsolete_bootstrap_commands_are_not_registered():
    parser = lpm.build_parser()
    subcommands = next(
        action.choices
        for action in parser._actions
        if hasattr(action, "choices") and isinstance(action.choices, dict)
    )
    for command in REMOVED:
        assert command not in subcommands


def test_core_commands_remain_available():
    parser = lpm.build_parser()
    subcommands = next(
        action.choices
        for action in parser._actions
        if hasattr(action, "choices") and isinstance(action.choices, dict)
    )
    for command in (
        "buildpkg", "splitpkg", "rebuild-stage2", "install",
        "installpkg", "removepkg", "genindex", "createiso",
    ):
        assert command in subcommands


def test_removed_commands_not_in_privilege_or_state_sets():
    for command in REMOVED:
        assert command not in lpm._PRIVILEGED_COMMANDS
        assert command not in lpm._STATE_COMMANDS
