from __future__ import annotations

from pathlib import Path

import pytest

from lpm import app


def test_metadata_parser_does_not_execute_top_level_commands(tmp_path: Path) -> None:
    victim = tmp_path / "host-file"
    victim.write_text("keep me", encoding="utf-8")
    recipe = tmp_path / "safe.lpmbuild"
    recipe.write_text(
        f"""
NAME=safe
VERSION=1.0
RELEASE=1
ARCH=noarch
REQUIRES=(
  bash
  "glibc>=2.40"
)
rm -f {victim}

prepare() {{
  rm -rf /
}}
""",
        encoding="utf-8",
    )

    scalars, arrays, _maps = app._capture_lpmbuild_metadata(recipe)

    assert victim.read_text(encoding="utf-8") == "keep me"
    assert scalars["NAME"] == "safe"
    assert scalars["VERSION"] == "1.0"
    assert arrays["REQUIRES"] == ["bash", "glibc>=2.40"]


def test_metadata_parser_rejects_command_substitution(tmp_path: Path) -> None:
    recipe = tmp_path / "unsafe.lpmbuild"
    recipe.write_text(
        "NAME=unsafe\nVERSION=$(touch should-not-exist)\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="substitution is not allowed"):
        app._capture_lpmbuild_metadata(recipe)

    assert not (tmp_path / "should-not-exist").exists()
