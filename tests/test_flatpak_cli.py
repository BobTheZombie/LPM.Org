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



def test_inventory_reads_system_and_user_flatpaks_without_lpm_db():
    output = (
        "Application ID\tVersion\tArch\tOrigin\tInstallation\n"
        "org.mozilla.firefox\t140.0\tx86_64\tflathub\tsystem\n"
        "org.kde.krita\t5.2\tx86_64\tflathub\tuser\n"
    )
    with patch.object(flatpak_cli.shutil, "which", return_value="/usr/bin/flatpak"), \
         patch.object(flatpak_cli.subprocess, "run",
                      return_value=SimpleNamespace(returncode=0, stdout=output, stderr="")) as run:
        assert flatpak_cli.list_installed_flatpaks() == [
            ("org.kde.krita", "5.2", "x86_64", "flathub", "user"),
            ("org.mozilla.firefox", "140.0", "x86_64", "flathub", "system"),
        ]
    run.assert_called_once_with(
        ["/usr/bin/flatpak", "list", "--app",
         "--columns=application:full,version:full,arch:full,origin:full,installation:full"],
        check=False, stdout=flatpak_cli.subprocess.PIPE,
        stderr=flatpak_cli.subprocess.PIPE, text=True, timeout=30,
    )


def test_inventory_absent_flatpak_is_optional():
    with patch.object(flatpak_cli.shutil, "which", return_value=None):
        assert flatpak_cli.list_installed_flatpaks() is None


def test_inventory_nonzero_error_is_not_a_false_empty_list():
    with patch.object(flatpak_cli.shutil, "which", return_value="/usr/bin/flatpak"), \
         patch.object(flatpak_cli.subprocess, "run", return_value=SimpleNamespace(
             returncode=1, stdout="", stderr="Flatpak installation unavailable\n")):
        with pytest.raises(RuntimeError, match="Flatpak installation unavailable"):
            flatpak_cli.list_installed_flatpaks()


def test_inventory_rejects_unexpected_output():
    with patch.object(flatpak_cli.shutil, "which", return_value="/usr/bin/flatpak"), \
         patch.object(flatpak_cli.subprocess, "run", return_value=SimpleNamespace(
             returncode=0, stdout="badly formatted row\n", stderr="")):
        with pytest.raises(RuntimeError, match="Unexpected Flatpak list output"):
            flatpak_cli.list_installed_flatpaks()


def test_lpm_list_combines_native_and_flatpak_inventory(capsys):
    native = SimpleNamespace(
        execute=lambda *_: [("lpm", "1.0", "1", "x86_64", 0, 1)],
        close=lambda: None,
    )
    with patch.object(app, "db", return_value=native), \
         patch.object(flatpak_cli, "list_installed_flatpaks", return_value=[
             ("org.kde.krita", "5.2", "x86_64", "flathub", "user")
         ]):
        args = app.build_parser().parse_args(["list"])
        args.func(args)
    output = capsys.readouterr().out
    assert "Installed native packages: 1 total" in output
    assert "lpm" in output
    assert "Flatpak applications: 1 total" in output
    assert "flatpak:org.kde.krita" in output
    assert "user" in output
    assert "flathub" in output


def test_lpm_list_flatpak_only_does_not_access_native_db(capsys):
    with patch.object(app, "db", side_effect=AssertionError("native DB accessed")), \
         patch.object(flatpak_cli, "list_installed_flatpaks", return_value=[
             ("org.mozilla.firefox", "140.0", "x86_64", "flathub", "system")
         ]):
        args = app.build_parser().parse_args(["list", "--flatpak"])
        args.func(args)
    output = capsys.readouterr().out
    assert "Flatpak applications: 1 total" in output
    assert "Installed native packages" not in output


def test_lpm_list_native_only_does_not_query_flatpak(capsys):
    native = SimpleNamespace(execute=lambda *_: [], close=lambda: None)
    with patch.object(app, "db", return_value=native), \
         patch.object(flatpak_cli, "list_installed_flatpaks",
                      side_effect=AssertionError("Flatpak called")):
        args = app.build_parser().parse_args(["list", "--native"])
        args.func(args)
    assert "No native packages installed." in capsys.readouterr().out


def test_lpm_list_flatpak_only_reports_missing_flatpak():
    with patch.object(app, "db", side_effect=AssertionError("native DB accessed")), \
         patch.object(flatpak_cli, "list_installed_flatpaks", return_value=None):
        args = app.build_parser().parse_args(["list", "--flatpak"])
        with pytest.raises(SystemExit, match="Flatpak is not installed"):
            args.func(args)


def test_lpm_list_disallows_conflicting_filters():
    with pytest.raises(SystemExit):
        app.build_parser().parse_args(["list", "--native", "--flatpak"])



def test_native_style_flatpak_install_routed_before_native_state():
    with patch.object(app, "_resolve_lpm_attr",
                      side_effect=AssertionError("native state initialized")), \
         patch.object(flatpak_cli.shutil, "which", return_value="/usr/bin/flatpak"), \
         patch.object(flatpak_cli.subprocess, "run",
                      return_value=SimpleNamespace(returncode=0)) as run:
        assert app.main(["install", "--yes", "flatpak:org.mozilla.firefox"]) == 0
    run.assert_called_once_with(
        ["/usr/bin/flatpak", "install", "--system", "--assumeyes",
         "flathub", "org.mozilla.firefox"],
        check=False,
    )


def test_native_style_flatpak_remove_is_not_native_removal():
    with patch.object(app, "_resolve_lpm_attr",
                      side_effect=AssertionError("native state initialized")), \
         patch.object(flatpak_cli.shutil, "which", return_value="/usr/bin/flatpak"), \
         patch.object(flatpak_cli.subprocess, "run",
                      return_value=SimpleNamespace(returncode=0)) as run:
        assert app.main(["remove", "--yes", "flatpak:org.kde.krita"]) == 0
    run.assert_called_once_with(
        ["/usr/bin/flatpak", "uninstall", "--system", "--assumeyes", "org.kde.krita"],
        check=False,
    )


def test_native_style_flatpak_search_routes_to_flatpak():
    with patch.object(app, "_resolve_lpm_attr",
                      side_effect=AssertionError("native state initialized")), \
         patch.object(flatpak_cli.shutil, "which", return_value="/usr/bin/flatpak"), \
         patch.object(flatpak_cli.subprocess, "run",
                      return_value=SimpleNamespace(returncode=0)) as run:
        assert app.main(["search", "--flatpak", "web", "browser"]) == 0
    run.assert_called_once_with(["/usr/bin/flatpak", "search", "web browser"], check=False)


def test_update_flatpak_alias_supports_user_scope():
    with patch.object(app, "_resolve_lpm_attr",
                      side_effect=AssertionError("native state initialized")), \
         patch.object(flatpak_cli.shutil, "which", return_value="/usr/bin/flatpak"), \
         patch.object(flatpak_cli.subprocess, "run",
                      return_value=SimpleNamespace(returncode=0)) as run:
        assert app.main(["update", "--flatpak", "--user", "--yes"]) == 0
    run.assert_called_once_with(
        ["/usr/bin/flatpak", "update", "--user", "--assumeyes"],
        check=False,
    )


@pytest.mark.parametrize("args", [
    ["install", "nano", "flatpak:org.mozilla.firefox"],
    ["remove", "nano", "flatpak:org.mozilla.firefox"],
    ["install", "flatpak:org.mozilla.firefox", "--root", "/mnt"],
    ["remove", "flatpak:org.mozilla.firefox", "--dry-run"],
    ["update"],
    ["search", "--flatpak"],
])
def test_flatpak_alias_rejects_mixed_or_ambiguous_operations(args):
    with pytest.raises(SystemExit):
        app.main(args)


def test_flatpak_only_main_skips_native_first_run():
    with patch.object(app, "_resolve_lpm_attr",
                      side_effect=AssertionError("native state initialized")), \
         patch.object(flatpak_cli, "list_installed_flatpaks", return_value=[]):
        assert app.main(["list", "--flatpak"]) is None
