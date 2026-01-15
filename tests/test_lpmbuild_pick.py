import importlib
import os
import sys
import textwrap
from pathlib import Path

import pytest


def _write_stub_modules(stub_dir: Path) -> None:
    stub_dir.mkdir(parents=True, exist_ok=True)
    (stub_dir / "tqdm.py").write_text(
        textwrap.dedent(
            """
            class tqdm:
                def __init__(self, iterable=None, **kwargs):
                    self.iterable = iterable or []
                    self.total = kwargs.get("total")
                    self.desc = kwargs.get("desc")
                    self.n = 0

                def __iter__(self):
                    for item in self.iterable:
                        self.n += 1
                        yield item

                def update(self, n=1):
                    self.n += n

                def set_description(self, desc):
                    self.desc = desc

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False
            """
        ),
        encoding="utf-8",
    )


def _import_lpm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    stub_dir = tmp_path / "stubs"
    _write_stub_modules(stub_dir)

    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    new_pythonpath = os.pathsep.join(filter(None, [str(stub_dir), existing_pythonpath]))
    monkeypatch.setenv("PYTHONPATH", new_pythonpath)
    if str(stub_dir) not in sys.path:
        sys.path.insert(0, str(stub_dir))

    for mod in ("tqdm", "lpm", "src.config"):
        sys.modules.pop(mod, None)

    return importlib.import_module("lpm")


@pytest.fixture
def lpm_module(tmp_path, monkeypatch):
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    return _import_lpm(tmp_path, monkeypatch)


def _write_lpmbuild_script(tmp_path: Path, name: str, staging_lines: list[str]) -> Path:
    script = tmp_path / f"{name}.lpmbuild"
    script.write_text(
        "\n".join(
            [
                f"NAME={name}",
                "VERSION=1.2.3",
                "RELEASE=1",
                "ARCH=noarch",
                "SUMMARY=\"Pick test package\"",
                "prepare(){ :; }",
                "build(){ :; }",
                "staging(){",
                *[f"  {line}" for line in staging_lines],
                "}",
            ]
        ),
        encoding="utf-8",
    )
    return script


def test_pick_moves_relative_path(tmp_path, lpm_module):
    name = "pick-relative"
    script = _write_lpmbuild_script(
        tmp_path,
        name,
        [
            "mkdir -p \"$pkgdir/usr/include\"",
            "echo header > \"$pkgdir/usr/include/a.h\"",
            "_pick soup2 usr/include/a.h",
        ],
    )

    lpm_module.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    srcroot = Path(f"/tmp/src-{name}")
    moved = srcroot / "soup2/usr/include/a.h"
    assert moved.exists()
    assert moved.read_text(encoding="utf-8").strip() == "header"
    assert not (Path(f"/tmp/pkg-{name}") / "usr/include/a.h").exists()


def test_pick_moves_absolute_path(tmp_path, lpm_module):
    name = "pick-absolute"
    script = _write_lpmbuild_script(
        tmp_path,
        name,
        [
            "mkdir -p \"$pkgdir/usr/lib\"",
            "echo library > \"$pkgdir/usr/lib/libx.so\"",
            "_pick soup2 \"$pkgdir/usr/lib/libx.so\"",
        ],
    )

    lpm_module.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    srcroot = Path(f"/tmp/src-{name}")
    moved = srcroot / "soup2/usr/lib/libx.so"
    assert moved.exists()
    assert moved.read_text(encoding="utf-8").strip() == "library"
    assert not (Path(f"/tmp/pkg-{name}") / "usr/lib/libx.so").exists()


def test_pick_moves_globbed_paths(tmp_path, lpm_module):
    name = "pick-glob"
    script = _write_lpmbuild_script(
        tmp_path,
        name,
        [
            "mkdir -p \"$pkgdir/usr/lib\"",
            "echo library > \"$pkgdir/usr/lib/libx.so\"",
            "echo library1 > \"$pkgdir/usr/lib/libx.so.1\"",
            "_pick soup2 usr/lib/libx.so*",
        ],
    )

    lpm_module.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    srcroot = Path(f"/tmp/src-{name}")
    moved_root = srcroot / "soup2/usr/lib"
    assert (moved_root / "libx.so").exists()
    assert (moved_root / "libx.so.1").exists()
    assert not (Path(f"/tmp/pkg-{name}") / "usr/lib/libx.so").exists()
    assert not (Path(f"/tmp/pkg-{name}") / "usr/lib/libx.so.1").exists()


def test_pick_prunes_empty_directories(tmp_path, lpm_module):
    name = "pick-cleanup"
    script = _write_lpmbuild_script(
        tmp_path,
        name,
        [
            "mkdir -p \"$pkgdir/usr/share/doc/p/q\"",
            "echo docs > \"$pkgdir/usr/share/doc/p/q/file\"",
            "echo keep > \"$pkgdir/usr/share/doc/keep.txt\"",
            "_pick soup2 usr/share/doc/p/q/file",
        ],
    )

    lpm_module.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    pkgdir = Path(f"/tmp/pkg-{name}")
    assert (pkgdir / "usr/share/doc").exists()
    assert (pkgdir / "usr/share/doc/keep.txt").exists()
    assert not (pkgdir / "usr/share/doc/p").exists()
    assert not (pkgdir / "usr/share/doc/p/q").exists()

    srcroot = Path(f"/tmp/src-{name}")
    moved = srcroot / "soup2/usr/share/doc/p/q/file"
    assert moved.exists()


@pytest.mark.parametrize(
    "case_name, staging_lines, expected_source_template",
    [
        (
            "relative-outside",
            [
                "mkdir -p \"$pkgdir\"",
                "echo outside > \"$pkgdir/../outside\"",
                "_pick soup2 ../outside",
            ],
            "/tmp/outside",
        ),
        (
            "absolute-outside",
            [
                "echo outside > \"$BUILDROOT/outside-abs\"",
                "_pick soup2 \"$BUILDROOT/outside-abs\"",
            ],
            "/tmp/build-{name}/outside-abs",
        ),
    ],
)
def test_pick_rejects_paths_outside_pkgdir(
    tmp_path, lpm_module, case_name, staging_lines, expected_source_template
):
    name = f"pick-{case_name}"
    script = _write_lpmbuild_script(tmp_path, name, staging_lines)

    with pytest.raises(SystemExit):
        lpm_module.run_lpmbuild(
            script,
            outdir=tmp_path,
            prompt_install=False,
            build_deps=False,
        )

    srcroot = Path(f"/tmp/src-{name}") / "soup2"
    assert not srcroot.exists()
    expected_source = Path(expected_source_template.format(name=name))
    assert expected_source.exists()
