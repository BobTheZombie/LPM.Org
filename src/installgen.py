from __future__ import annotations

from pathlib import Path
import os


def _escape_double_quotes(value: str) -> str:
    """Escape a string for safe inclusion inside double quotes."""

    return value.replace("\\", "\\\\").replace('"', '\\"')


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

    for link in stagedir.rglob("*"):
        try:
            if not link.is_symlink():
                continue
        except OSError:
            continue

        try:
            target = os.readlink(link)
        except OSError:
            continue

        if not target.startswith("/"):
            continue

        try:
            rel_path = link.relative_to(stagedir).as_posix()
            parent_rel = link.parent.relative_to(stagedir).as_posix()
        except ValueError:
            continue

        start = parent_rel or "."
        rel_target = os.path.relpath(target.lstrip("/"), start)

        dest_expr = f"${{LPM_ROOT:-/}}/{rel_path}"
        dest_quoted = _escape_double_quotes(dest_expr)
        target_quoted = _escape_double_quotes(rel_target)

        cmds.append(
            f'[ -L "{dest_quoted}" ] && ln -snf "{target_quoted}" "{dest_quoted}"'
        )

    return "\n".join(cmds)


__all__ = ["generate_install_script"]
