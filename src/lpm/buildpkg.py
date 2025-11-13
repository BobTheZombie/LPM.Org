from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - imported for type checking only
    from argparse import Namespace


def _get_buildpkg_worker_count(conf) -> int:
    value = conf.get("BUILDPKG_WORKERS")
    if value is not None:
        try:
            workers = int(value)
        except (TypeError, ValueError):
            workers = 0
        else:
            if workers > 0:
                return workers

    cpu_workers = os.cpu_count() or 1
    return max(2, min(8, cpu_workers))


def cmd_buildpkg(a: "Namespace") -> None:
    from .app import (
        CONF,
        _parse_cpu_overrides,
        _resolve_lpm_attr,
        build_python_package_from_pip,
        die,
        ok,
        print_build_summary,
        prompt_install_pkg,
        read_package_meta,
        run_lpmbuild,
    )

    worker_count = _get_buildpkg_worker_count(CONF)
    cpu_override = _parse_cpu_overrides(getattr(a, "overrides", []))

    if a.python_pip:
        if a.script:
            die("Cannot specify both a .lpmbuild script and --python-pip")
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future = executor.submit(
                _resolve_lpm_attr("build_python_package_from_pip", build_python_package_from_pip),
                a.python_pip,
                a.outdir,
                include_deps=not a.no_deps,
                cpu_overrides=cpu_override,
            )
            out, meta, duration = future.result()
        _resolve_lpm_attr("prompt_install_pkg", prompt_install_pkg)(out, default=a.install_default)
        print_build_summary(meta, out, duration, len(meta.requires), 1)
        ok(f"Built {out}")
        return

    if not a.script:
        die("buildpkg requires a .lpmbuild script or --python-pip")

    script_path = Path(a.script)
    if not script_path.exists():
        die(f".lpmbuild script not found: {script_path}")

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future = executor.submit(
            run_lpmbuild,
            script_path,
            a.outdir,
            build_deps=not a.no_deps,
            force_rebuild=a.force_rebuild,
            prompt_default=a.install_default,
            executor=executor if worker_count > 1 else None,
            cpu_overrides=cpu_override,
        )
        out, duration, phases, splits = future.result()

    if out and out.exists():
        meta, _ = read_package_meta(out)
        print_build_summary(meta, out, duration, len(meta.requires), phases)
        if splits:
            for spath, smeta in splits:
                ok(f"Split: {spath} ({smeta.name})")
        ok(f"Built {out}")
    else:
        die(f"Build failed for {a.script}")

