#!/usr/bin/env python3
"""Micro-benchmark for dependency scanning during ``run_lpmbuild``."""

import os
import sys
import tempfile
import textwrap
import timeit
from pathlib import Path
from unittest import mock


def _prepare_lpmbuild(tmpdir: Path, deps) -> Path:
    script = tmpdir / "bench.lpmbuild"
    script.write_text(
        textwrap.dedent(
            """
            NAME=bench
            VERSION=1
            RELEASE=1
            ARCH=noarch
            REQUIRES=({deps})
            prepare() {{ :; }}
            build() {{ :; }}
            staging() {{ :; }}
            """
        ).format(deps=" ".join(deps))
    )
    return script


def main():
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))

    if "tqdm" not in sys.modules:
        import types

        module = types.ModuleType("tqdm")

        class _DummyTqdm:
            def __init__(self, iterable=None, total=None, **kwargs):
                self.iterable = iterable or []
                self.total = total
                self.n = 0

            def __iter__(self):
                for item in self.iterable:
                    self.n += 1
                    yield item

            def update(self, n=1):
                self.n += n

            def set_description(self, _desc):
                return None

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        module.tqdm = _DummyTqdm  # type: ignore[attr-defined]

        sys.modules["tqdm"] = module

    import lpm

    deps = [f"dep{i}" for i in range(100)]

    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        os.environ["LPM_STATE_DIR"] = str(tmpdir / "state")
        script = _prepare_lpmbuild(tmpdir, deps)

        calls = {"db": 0, "db_installed": 0, "close": 0}

        class DummyConn:
            def close(self):
                calls["close"] += 1

        def fake_db():
            calls["db"] += 1
            return DummyConn()

        def fake_db_installed(conn):
            calls["db_installed"] += 1
            return {dep: {} for dep in deps}

        patches = [
            mock.patch.object(lpm, "sandboxed_run", lambda *args, **kwargs: None),
            mock.patch.object(lpm, "generate_install_script", lambda stagedir: "echo hi"),
            mock.patch.object(lpm, "build_package", lambda stagedir, meta, out, sign=True: out.write_text("pkg")),
            mock.patch.object(lpm, "db", fake_db),
            mock.patch.object(lpm, "db_installed", fake_db_installed),
        ]

        with ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)

            def run_once():
                out_path, _, _, _ = lpm.run_lpmbuild(
                    script,
                    outdir=tmpdir,
                    prompt_install=False,
                    build_deps=True,
                )
                out_path.unlink(missing_ok=True)

            total = timeit.timeit(run_once, number=25)

        print(f"Average runtime: {total / 25:.6f}s over 25 iterations with {len(deps)} deps")
        print(
            "Database connections per iteration: "
            f"{calls['db']} (db_installed={calls['db_installed']}, closed={calls['close']})"
        )


if __name__ == "__main__":
    from contextlib import ExitStack

    main()
