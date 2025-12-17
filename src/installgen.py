from __future__ import annotations

from pathlib import Path
import os
import textwrap


def _escape_double_quotes(value: str) -> str:
    """Escape a string for safe inclusion inside double quotes."""

    return value.replace("\\", "\\\\").replace('"', '\\"')


def generate_install_script(stagedir: Path) -> str:
    """Return the default embedded install script body.

    The script opts into strict error handling, provides structured logging,
    and refreshes post-install caches only when the necessary tools, targets,
    and privileges are present. Operations that require root use ``run_as_root``
    for clearer diagnostics instead of failing with obscure permission errors.
    """

    stagedir = stagedir.resolve()
    cmds: list[str] = []

    apps_dir = stagedir / "usr/share/applications"
    if apps_dir.is_dir() and any(apps_dir.rglob("*.desktop")):
        rel = apps_dir.relative_to(stagedir).as_posix()
        cmds.append(
            textwrap.dedent(
                f"""
                if command -v update-desktop-database >/dev/null 2>&1; then
                  if ! run_as_root update-desktop-database "${{ROOT}}/{rel}"; then
                    log_warn "desktop database not refreshed; rerun 'sudo update-desktop-database \\\"${{ROOT}}/{rel}\\\"'"
                  else
                    log_ok "updated desktop database"
                  fi
                else
                  log_warn "update-desktop-database not found; skipping desktop cache refresh"
                fi
                """
            ).strip()
        )

    icons_root = stagedir / "usr/share/icons"
    if icons_root.is_dir():
        for index in icons_root.glob("*/index.theme"):
            theme_dir = index.parent.relative_to(stagedir).as_posix()
            cmds.append(
                textwrap.dedent(
                    f"""
                    if command -v gtk-update-icon-cache >/dev/null 2>&1; then
                      if ! run_as_root gtk-update-icon-cache "${{ROOT}}/{theme_dir}"; then
                        log_warn "icon cache for '{theme_dir}' not refreshed; rerun 'sudo gtk-update-icon-cache "${{ROOT}}/{theme_dir}"'"
                      else
                        log_ok "updated icon cache for {theme_dir}"
                      fi
                    else
                      log_warn "gtk-update-icon-cache not found; skipping icon cache refresh"
                    fi
                    """
                ).strip()
            )

    lib_dirs: list[Path] = []
    for candidate in (stagedir / "usr/lib", stagedir / "usr/lib64"):
        if candidate.is_dir():
            lib_dirs.append(candidate)

    if any(p.is_file() for d in lib_dirs for p in d.rglob("*.so*")):
        cmds.append("update_ld_cache")

    gio_candidates = [
        stagedir / "usr/lib/gio/modules",
        stagedir / "usr/lib64/gio/modules",
    ]
    if any(path.is_dir() for path in gio_candidates):
        cmds.append("update_gio_modules_cache")

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

        dest_expr = f"${{ROOT}}/{rel_path}"
        dest_quoted = _escape_double_quotes(dest_expr)
        target_quoted = _escape_double_quotes(rel_target)

        cmds.append(
            textwrap.dedent(
                f"""
                if ! run_as_root ln -snf "{target_quoted}" "{dest_quoted}"; then
                  log_warn "could not retarget absolute symlink {rel_path}"
                fi
                """
            ).strip()
        )

    body = "\n\n".join(cmds)

    script = textwrap.dedent(
        f"""
        #!/bin/bash
        set -euo pipefail

        log_ok() {{ echo "[OK] $*" >&2; }}
        log_warn() {{ echo "[warn] $*" >&2; }}
        log_error() {{ echo "[ERROR] $*" >&2; }}
        log_info() {{ echo "[lpm] $*" >&2; }}

        ROOT="${{LPM_ROOT:-/}}"
        ROOT="${{ROOT%/}}"
        ROOT="${{ROOT:-/}}"

        run_as_root() {{
          if [ "$(id -u)" -eq 0 ]; then
            "$@"
            return $?
          fi
          log_error "root privileges required to run: $*"
          return 1
        }}

        update_gio_modules_cache() {{
          if ! command -v gio-querymodules >/dev/null 2>&1; then
            log_warn "gio-querymodules not found; skipping gio module cache refresh"
            return 0
          fi

          local module_dir=""
          for candidate in "${{ROOT}}/usr/lib/gio/modules" "${{ROOT}}/usr/lib64/gio/modules"; do
            if [ -d "$candidate" ]; then
              module_dir="$candidate"
              break
            fi
          done

          if [ -z "$module_dir" ]; then
            log_warn "no gio module directory found under $ROOT; skipping"
            return 0
          fi

          if [ "$(id -u)" -ne 0 ]; then
            log_warn "gio module cache not refreshed (requires root). Run 'sudo gio-querymodules "$module_dir"' after install."
            return 0
          fi

          if gio-querymodules "$module_dir"; then
            log_ok "updated gio module cache ($module_dir)"
          else
            log_warn "gio-querymodules failed for $module_dir"
          fi
        }}

        update_ld_cache() {{
          if [ "${{ROOT}}" != "/" ]; then
            log_warn "ldconfig skipped for non-root prefix $ROOT"
            return 0
          fi

          if ! command -v ldconfig >/dev/null 2>&1; then
            return 0
          fi

          if [ "$(id -u)" -ne 0 ]; then
            log_warn "ldconfig skipped (requires root). Run 'sudo ldconfig' after install."
            return 0
          fi

          if ldconfig; then
            log_ok "refreshed dynamic linker cache"
          else
            log_warn "ldconfig failed"
          fi
        }}

        {body}
        """
    ).strip()

    if not script.endswith("\n"):
        script += "\n"

    return script


__all__ = ["generate_install_script"]
