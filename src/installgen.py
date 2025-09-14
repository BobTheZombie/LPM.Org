from __future__ import annotations

from pathlib import Path


def generate_install_script(stagedir: Path) -> str:
    cmds: list[str] = []

    apps_dir = stagedir / "usr/share/applications"
    if apps_dir.is_dir() and any(apps_dir.rglob("*.desktop")):
        rel = apps_dir.relative_to(stagedir).as_posix()
        cmds.append(
            "command -v update-desktop-database >/dev/null 2>&1 "
            f"&& update-desktop-database \"${{LPM_ROOT:-/}}/{rel}\" || true"
        )

    icons_root = stagedir / "usr/share/icons"
    if icons_root.is_dir():
        for index in icons_root.glob("*/index.theme"):
            theme_dir = index.parent.relative_to(stagedir).as_posix()
            cmds.append(
                "command -v gtk-update-icon-cache >/dev/null 2>&1 "
                f"&& gtk-update-icon-cache \"${{LPM_ROOT:-/}}/{theme_dir}\" || true"
            )

    lib_dir = stagedir / "usr/lib"
    if lib_dir.is_dir() and any(p.is_file() for p in lib_dir.rglob("*.so*")):
        cmds.append(
            "[ \"${LPM_ROOT:-/}\" = \"/\" ] && command -v ldconfig >/dev/null 2>&1 "
            "&& ldconfig || true"
        )

    return "\n".join(cmds)


__all__ = ["generate_install_script"]
