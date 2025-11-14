import sys
from pathlib import Path


# Allow importing from the src directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lpm.installgen import generate_install_script


def test_desktop_file_triggers_update_desktop_database(tmp_path):
    stage = tmp_path / "stage"
    apps = stage / "usr/share/applications"
    apps.mkdir(parents=True)
    (apps / "foo.desktop").write_text("[Desktop Entry]")

    script = generate_install_script(stage)

    assert (
        script
        == 'command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "${LPM_ROOT:-/}/usr/share/applications" || true'
    )


def test_icon_theme_triggers_icon_cache_update(tmp_path):
    stage = tmp_path / "stage"
    theme = stage / "usr/share/icons/hicolor"
    theme.mkdir(parents=True)
    (theme / "index.theme").write_text("[Icon Theme]")

    script = generate_install_script(stage)

    assert (
        script
        == 'command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache "${LPM_ROOT:-/}/usr/share/icons/hicolor" || true'
    )


def test_shared_library_triggers_ldconfig(tmp_path):
    stage = tmp_path / "stage"
    libdir = stage / "usr/lib"
    libdir.mkdir(parents=True)
    (libdir / "libfoo.so").write_text("")

    script = generate_install_script(stage)

    assert script == '[ "${LPM_ROOT:-/}" = "/" ] && command -v ldconfig >/dev/null 2>&1 && ldconfig || true'

