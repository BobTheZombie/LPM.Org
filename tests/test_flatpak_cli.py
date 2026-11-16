"""Flatpak integration never invokes native LPM transactions."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lpm import app
from lpm import flatpak_cli


def _parse(*tokens):
    return app.build_parser().parse_args(["flatpak", *tokens])


def test_flatpak_install_uses_flathub_and_system_scope():
    args = _parse("install", "--yes", "org.mozilla.firefox")
    commands = []
    with patch.object(flatpak_cli.shutil, "which", return_value="/usr/bin/flatpak"), \
         patch.object(flatpak_cli.subprocess, "run", side_effect=lambda argv, **kw: (
             commands.append((argv, kw)) or SimpleNamespace(returncode=0)
         )):
        assert args.func(args) == 0
    assert commands == [(
        ["/usr/bin/flatpak", "install", "--system", "--assumeyes",
         "flathub", "org.mozilla.firefox"],
        {"check": False},
    )]


def test_user_scope_and_no_lpm_state_required(tmp_path, monkeypatch):
    args = _parse("install", "--user", "org.kde.krita")
    with patch.object(flatpak_cli.shutil, "which", return_value="/usr/bin/flatpak"), \
         patch.object(flatpak_cli.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
        assert args.func(args) == 0
    run.assert_called_once_with(
        ["/usr/bin/flatpak", "install", "--user", "flathub", "org.kde.krita"],
        check=False,
    )


def test_setup_is_idempotent_and_uses_https_flathub_repo():
    args = _parse("setup")
    with patch.object(flatpak_cli.shutil, "which", return_value="/usr/bin/flatpak"), \
         patch.object(flatpak_cli.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
        args.func(args)
    run.assert_called_once_with([
        "/usr/bin/flatpak", "remote-add", "--system", "--if-not-exists",
        "--from", "flathub", flatpak_cli.FLATHUB_REPO,
    ], check=False)


@pytest.mark.parametrize("tokens", [
    ("install", "--user", "--system", "org.kde.krita"),
    ("install",),
    ("search",),
])
def test_invalid_parser_combinations(tokens):
    with pytest.raises(SystemExit):
        _parse(*tokens)


@pytest.mark.parametrize("id_", ["--system", "../bad", "https://evil.test", "org.foo;rm",
                                  "org.foo/../bad"])
def test_rejects_bad_app_ids(id_):
    with pytest.raises(SystemExit):
        flatpak_cli._validate_apps([id_])


def test_nonzero_exit_propagates_to_caller():
    args = _parse("update")
    with patch.object(flatpak_cli.shutil, "which", return_value="/usr/bin/flatpak"), \
         patch.object(flatpak_cli.subprocess, "run", return_value=SimpleNamespace(returncode=42)):
        with pytest.raises(SystemExit) as err:
            args.func(args)
    assert err.value.code == 42


def test_missing_flatpak_explains_required_packages():
    args = _parse("list")
    with patch.object(flatpak_cli.shutil, "which", return_value=None):
        with pytest.raises(SystemExit, match="lpm install flatpak flatpak-flathub"):
            args.func(args)


def test_search_no_extra_remote_or_shell():
    args = _parse("search", "firefox", "web")
    with patch.object(flatpak_cli.shutil, "which", return_value="/usr/bin/flatpak"), \
         patch.object(flatpak_cli.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
        args.func(args)
    run.assert_called_once_with(["/usr/bin/flatpak", "search", "firefox web"], check=False)
