from __future__ import annotations

from pathlib import Path
import os


def _escape_double_quotes(value: str) -> str:
    """Escape a string for safe inclusion inside double quotes."""

    return value.replace("\\", "\\\\").replace('"', '\\"')


def generate_install_script(stagedir: Path) -> str:
    """Return a minimal install script for staged content."""

    stagedir = stagedir.resolve()
    cmds: list[str] = []

    apps_dir = stagedir / "usr/share/applications"
    if apps_dir.is_dir() and any(apps_dir.rglob("*.desktop")):
        rel = apps_dir.relative_to(stagedir).as_posix()
        cmds.append(
            f"command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database \"${{LPM_ROOT:-/}}/{rel}\" || true"
        )

    icons_root = stagedir / "usr/share/icons"
    if icons_root.is_dir():
        for index in icons_root.glob("*/index.theme"):
            theme_dir = index.parent.relative_to(stagedir).as_posix()
            cmds.append(
                f"command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache \"${{LPM_ROOT:-/}}/{theme_dir}\" || true"
            )

    lib_dirs: list[Path] = []
    for candidate in (stagedir / "usr/lib", stagedir / "usr/lib64"):
        if candidate.is_dir():
            lib_dirs.append(candidate)

    has_shared_libs = any(p.is_file() for d in lib_dirs for p in d.rglob("*.so*"))
    if has_shared_libs:
        cmds.append(
            '[ "${LPM_ROOT:-/}" = "/" ] && command -v ldconfig >/dev/null 2>&1 && ldconfig || true'
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

        cmds.append(f'ln -snf "{_escape_double_quotes(rel_target)}" "{_escape_double_quotes(dest_expr)}"')

    gio_candidates = [
        stagedir / "usr/lib/gio/modules",
        stagedir / "usr/lib64/gio/modules",
    ]
    needs_gio_refresh = any(path.is_dir() for path in gio_candidates)

    needs_extended = needs_gio_refresh

    if not needs_extended:
        return "\n".join(cmds) if cmds else ":"

    lines: list[str] = [
        "#!/bin/sh",
        "set -eu",
        "",
        'log_ok() { echo "[OK] $*" >&2; }',
        'log_warn() { echo "[warn] $*" >&2; }',
        'ROOT="${LPM_ROOT:-/}"',
        'ROOT="${ROOT%/}"',
        'ROOT="${ROOT:-/}"',
        "",
    ]

    if needs_gio_refresh:
        gio_dirs = " ".join(
            f'"{Path("${ROOT}") / Path(p).relative_to(stagedir)}"'
            for p in gio_candidates
            if p.is_dir()
        )
        lines.extend(
            [
                "if command -v gio-querymodules >/dev/null 2>&1; then",
                "  if [ \"$(id -u)\" -ne 0 ]; then",
                "    log_warn \"gio module cache not refreshed (requires root). Run 'sudo gio-querymodules \"${ROOT}/usr/lib64/gio/modules\"' after install.\"",
                "  else",
                f"    gio-querymodules {gio_dirs} || log_warn \"gio-querymodules failed\"",
                "  fi",
                "else",
                "  log_warn \"gio-querymodules not found; skipping gio module cache refresh\"",
                "fi",
                "",
            ]
        )

    if has_shared_libs:
        lines.extend(
            [
                "if [ \"${ROOT}\" != \"/\" ]; then",
                "  log_warn \"ldconfig skipped for non-root prefix ${ROOT}\"",
                "elif ! command -v ldconfig >/dev/null 2>&1; then",
                "  :",
                "elif [ \"$(id -u)\" -ne 0 ]; then",
                "  log_warn \"ldconfig skipped (requires root). Run 'sudo ldconfig' after install.\"",
                "else",
                "  if ! ldconfig; then",
                "    log_warn \"ldconfig failed\"",
                "  else",
                "    log_ok \"refreshed dynamic linker cache\"",
                "  fi",
                "fi",
                "",
            ]
        )

    lines.extend(cmds)

    script = "\n".join(lines)
    if not script.endswith("\n"):
        script += "\n"

    return script


__all__ = ["generate_install_script"]
