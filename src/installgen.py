from __future__ import annotations

import os
from pathlib import Path


def _escape_double_quotes(value: str) -> str:
    """Escape a string for safe inclusion inside double quotes."""

    return value.replace("\\", "\\\\").replace('"', '\\"')


def _build_simple_commands(stagedir: Path) -> list[str]:
    cmds: list[str] = []

    apps_dir = stagedir / "usr/share/applications"
    if apps_dir.is_dir() and any(apps_dir.rglob("*.desktop")):
        rel = apps_dir.relative_to(stagedir).as_posix()
        cmds.append(
            'command -v update-desktop-database >/dev/null 2>&1 '
            f'&& update-desktop-database "${{LPM_ROOT:-/}}/{rel}" || true'
        )

    icons_root = stagedir / "usr/share/icons"
    if icons_root.is_dir():
        for index in icons_root.glob("*/index.theme"):
            theme_dir = index.parent.relative_to(stagedir).as_posix()
            cmds.append(
                'command -v gtk-update-icon-cache >/dev/null 2>&1 '
                f'&& gtk-update-icon-cache "${{LPM_ROOT:-/}}/{theme_dir}" || true'
            )

    lib_dirs: list[Path] = []
    for candidate in (stagedir / "usr/lib", stagedir / "usr/lib64"):
        if candidate.is_dir():
            lib_dirs.append(candidate)

    if any(p.is_file() for d in lib_dirs for p in d.rglob("*.so*")):
        cmds.append(
            '[ "${LPM_ROOT:-/}" = "/" ] && command -v ldconfig >/dev/null 2>&1 && ldconfig || true'
        )

    return cmds


def _find_absolute_symlinks(stagedir: Path) -> list[tuple[str, str]]:
    results: list[tuple[str, str]] = []
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
        results.append((rel_path, rel_target))

    return results


def generate_install_script(stagedir: Path) -> str:
    """Return the default embedded install script body."""

    helper_lines = [
        "# Helper functions for package install scripts.",
        "#",
        "# lpm_should_replace <new_full> <old_full> [action]",
        "#   Returns 0 (true) when content should be replaced during install/upgrade.",
        "#   Uses the provided action or LPM_INSTALL_ACTION, and falls back to comparing",
        "#   the new_full/old_full identifiers when no action is supplied.",
        "#",
        "# lpm_remove_path <path>",
        "#   Removes existing files, directories, or symlinks before writing new content.",
        "#   Uses rm -f for files/links and rm -rf for directories.",
        "",
        "lpm_should_replace() {",
        "  new_full_arg=${1:-${new_full:-}}",
        "  old_full_arg=${2:-${old_full:-}}",
        "  action_arg=${3:-${LPM_INSTALL_ACTION:-}}",
        "",
        "  if [ -n \"$action_arg\" ]; then",
        "    case \"$action_arg\" in",
        "      install)",
        "        return 0",
        "        ;;",
        "      upgrade)",
        "        if [ -z \"$old_full_arg\" ]; then",
        "          return 0",
        "        fi",
        "        if [ \"$new_full_arg\" != \"$old_full_arg\" ]; then",
        "          return 0",
        "        fi",
        "        ;;",
        "    esac",
        "    return 1",
        "  fi",
        "",
        "  if [ -n \"$new_full_arg\" ] && [ -n \"$old_full_arg\" ] && [ \"$new_full_arg\" != \"$old_full_arg\" ]; then",
        "    return 0",
        "  fi",
        "",
        "  return 1",
        "}",
        "",
        "lpm_remove_path() {",
        "  target=$1",
        "  if [ -z \"$target\" ]; then",
        "    return 1",
        "  fi",
        "",
        "  if [ -L \"$target\" ] || [ -f \"$target\" ]; then",
        "    rm -f \"$target\"",
        "  elif [ -d \"$target\" ]; then",
        "    rm -rf \"$target\"",
        "  fi",
        "}",
        "",
    ]

    stagedir = stagedir.resolve()
    simple_cmds = _build_simple_commands(stagedir)
    ldconfig_cmd = None
    for cmd in simple_cmds:
        if cmd.startswith("["):
            ldconfig_cmd = cmd
            break

    gio_candidates = [
        stagedir / "usr/lib/gio/modules",
        stagedir / "usr/lib64/gio/modules",
    ]
    has_gio = any(path.is_dir() for path in gio_candidates)
    absolute_symlinks = _find_absolute_symlinks(stagedir)

    needs_complex = has_gio or bool(absolute_symlinks)
    if not needs_complex:
        lines = [*helper_lines, *simple_cmds]
        if not simple_cmds:
            lines.append(":")
        return "\n".join(lines)

    if ldconfig_cmd is not None:
        simple_cmds = [cmd for cmd in simple_cmds if cmd is not ldconfig_cmd]

    lines: list[str] = [
        "#!/bin/sh",
        "set -e",
        "",
        'log_ok() { echo "[OK] $*" >&2; }',
        'log_warn() { echo "[warn] $*" >&2; }',
        'log_error() { echo "[ERROR] $*" >&2; }',
        "",
        'ROOT="${LPM_ROOT:-/}"',
        'ROOT="${ROOT%/}"',
        'ROOT="${ROOT:-/}"',
        "",
    ]
    lines.extend(helper_lines)

    if has_gio:
        lines.extend(
            [
                "update_gio_modules_cache() {",
                "  if ! command -v gio-querymodules >/dev/null 2>&1; then",
                "    log_warn \"gio-querymodules not found; skipping gio module cache refresh\"",
                "    return 0",
                "  fi",
                "",
                "  module_dir=\"\"",
                "  for candidate in \"$ROOT/usr/lib/gio/modules\" \"$ROOT/usr/lib64/gio/modules\"; do",
                "    if [ -d \"$candidate\" ]; then",
                "      module_dir=\"$candidate\"",
                "      break",
                "    fi",
                "  done",
                "",
                "  if [ -z \"$module_dir\" ]; then",
                "    log_warn \"no gio module directory found under $ROOT; skipping\"",
                "    return 0",
                "  fi",
                "",
                "  if [ \"$(id -u)\" -ne 0 ]; then",
                "    log_warn \"gio module cache not refreshed (requires root). Run 'sudo gio-querymodules \\\"$module_dir\\\"' after install.\"",
                "    return 0",
                "  fi",
                "",
                "  if gio-querymodules \"$module_dir\"; then",
                "    log_ok \"updated gio module cache ($module_dir)\"",
                "  else",
                "    log_warn \"gio-querymodules failed for $module_dir\"",
                "  fi",
                "}",
                "",
            ]
        )

    if ldconfig_cmd is not None:
        lines.extend(
            [
                "update_ld_cache() {",
                "  if [ \"$ROOT\" != \"/\" ]; then",
                "    log_warn \"ldconfig skipped for non-root prefix $ROOT\"",
                "    return 0",
                "  fi",
                "",
                "  if ! command -v ldconfig >/dev/null 2>&1; then",
                "    return 0",
                "  fi",
                "",
                "  if [ \"$(id -u)\" -ne 0 ]; then",
                "    log_warn \"ldconfig skipped (requires root). Run 'sudo ldconfig' after install.\"",
                "    return 0",
                "  fi",
                "",
                "  if ldconfig; then",
                "    log_ok \"refreshed dynamic linker cache\"",
                "  else",
                "    log_warn \"ldconfig failed\"",
                "  fi",
                "}",
                "",
            ]
        )

    if simple_cmds:
        lines.extend(simple_cmds)
        lines.append("")

    if absolute_symlinks:
        for rel_path, rel_target in absolute_symlinks:
            dest_expr = f"${{LPM_ROOT:-/}}/{rel_path}"
            dest_quoted = _escape_double_quotes(dest_expr)
            target_quoted = _escape_double_quotes(rel_target)
            lines.append(f'ln -snf "{target_quoted}" "{dest_quoted}" || true')
        lines.append("")

    if has_gio:
        lines.append("update_gio_modules_cache")
    if ldconfig_cmd is not None:
        lines.append("update_ld_cache")

    return "\n".join(line for line in lines if line is not None)


__all__ = ["generate_install_script"]
