from __future__ import annotations

from lpm import app as lpm


def test_install_parser_defines_force_default_for_lpminstall():
    parser = lpm.build_parser()

    args = parser.parse_args(["install", "demo.lpminstall"])

    assert args.func is lpm.cmd_install
    assert args.force is False


def test_install_parser_accepts_force():
    parser = lpm.build_parser()

    args = parser.parse_args(["install", "demo.lpminstall", "--force"])

    assert args.force is True
