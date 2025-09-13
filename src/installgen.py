from __future__ import annotations

from pathlib import Path


def generate_install_script(stagedir: Path) -> str:
    cmds: list[str] = []

    apps_dir = stagedir / "usr/share/applications"
    if apps_dir.is_dir() and any(apps_dir.rglob("*.desktop")):
        rel = apps_dir.relative_to(stagedir).as_posix()
        cmds.append(f"update-desktop-database \"${{LPM_ROOT:-/}}/{rel}\"")

    icons_root = stagedir / "usr/share/icons"
    if icons_root.is_dir():
        for index in icons_root.glob("*/index.theme"):
            theme_dir = index.parent.relative_to(stagedir).as_posix()
            cmds.append(f"gtk-update-icon-cache \"${{LPM_ROOT:-/}}/{theme_dir}\"")

    lib_dir = stagedir / "usr/lib"
    if lib_dir.is_dir() and any(p.is_file() for p in lib_dir.rglob("*.so*")):
        cmds.append("if [ \"${LPM_ROOT:-/}\" = \"/\" ]; then ldconfig; fi")

    return "\n".join(cmds)


__all__ = ["generate_install_script"]
