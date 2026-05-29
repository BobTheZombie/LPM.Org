from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from lpm import chroot_helpers


def test_buildgen_manifest_is_deterministic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "packages"
    for name in ("a", "b"):
        p = src / name / f"{name}.lpmbuild"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# stub\n", encoding="utf-8")

    from lpm import app as lpm_app

    def fake_capture(path: Path):
        if path.stem == "a":
            return {"NAME": "a", "VERSION": "1"}, {"REQUIRES": ["b"]}, {}
        return {"NAME": "b", "VERSION": "1"}, {"REQUIRES": []}, {}

    monkeypatch.setattr(lpm_app, "_capture_lpmbuild_metadata", fake_capture)

    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"
    args1 = Namespace(root=str(tmp_path / "root"), source=str(src), output_dir=str(out1), dry_run=False, verbose=False)
    args2 = Namespace(root=str(tmp_path / "root"), source=str(src), output_dir=str(out2), dry_run=False, verbose=False)

    assert chroot_helpers.run_buildgen(args1) == 0
    assert chroot_helpers.run_buildgen(args2) == 0

    m1 = json.loads((out1 / "build-manifest.json").read_text(encoding="utf-8"))
    m2 = json.loads((out2 / "build-manifest.json").read_text(encoding="utf-8"))
    assert m1["package_order"] == ["b", "a"]
    assert m1 == m2


def test_buildgen_cycle_detection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "packages"
    for name in ("a", "b"):
        p = src / name / f"{name}.lpmbuild"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("# stub\n", encoding="utf-8")

    from lpm import app as lpm_app

    def fake_capture(path: Path):
        if path.stem == "a":
            return {"NAME": "a", "VERSION": "1"}, {"REQUIRES": ["b"]}, {}
        return {"NAME": "b", "VERSION": "1"}, {"REQUIRES": ["a"]}, {}

    monkeypatch.setattr(lpm_app, "_capture_lpmbuild_metadata", fake_capture)
    args = Namespace(root=str(tmp_path / "root"), source=str(src), output_dir=str(tmp_path / "out"), dry_run=False, verbose=False)

    with pytest.raises(ValueError, match="Cycle detected in buildgen dependency graph"):
        chroot_helpers.run_buildgen(args)


def test_buildchroot_missing_script_fails(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "package_order": ["demo"],
        "packages": [{"name": "demo", "script": str(tmp_path / "missing.lpmbuild"), "depends": []}],
    }
    (out / "build-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    args = Namespace(
        root=str(tmp_path / "root"),
        source=str(tmp_path / "packages"),
        cache_dir=str(tmp_path / "cache"),
        output_dir=str(out),
        dry_run=False,
        verbose=False,
    )
    with pytest.raises(ValueError, match="Missing .lpmbuild scripts for build targets"):
        chroot_helpers.run_buildchroot(args)


def test_buildchroot_executes_build_inside_requested_chroot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "packages"
    script = source / "demo" / "demo.lpmbuild"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# stub\n", encoding="utf-8")

    output = tmp_path / "out"
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "package_order": ["demo"],
        "packages": [{"name": "demo", "script": str(script), "depends": []}],
    }
    (output / "build-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    root = tmp_path / "root"
    cache = tmp_path / "cache"
    built = cache / "demo.lpm"
    commands: list[list[str]] = []
    mounted_api: list[Path] = []
    unmounted_api: list[Path] = []

    def fake_mount_api(target: Path, state: chroot_helpers.ChrootMountState) -> chroot_helpers.ChrootMountState:
        mounted_api.append(Path(target))
        state.mark_mounted("proc")
        return state

    def fake_umount_api(target: Path, state: chroot_helpers.ChrootMountState) -> chroot_helpers.ChrootMountState:
        unmounted_api.append(Path(target))
        state.mounted.clear()
        return state

    def fake_run(cmd, check=False, text=False, capture_output=False):
        cmd = [str(part) for part in cmd]
        commands.append(cmd)
        if cmd[:2] == ["mount", "--bind"]:
            return chroot_helpers.subprocess.CompletedProcess(cmd, 0)
        if cmd[0] == "umount":
            return chroot_helpers.subprocess.CompletedProcess(cmd, 0)
        if cmd[:2] == ["chroot", str(root)]:
            assert cmd[2:5] == ["python3", "-c", cmd[4]]
            assert cmd[-2] == "/tmp/lpm-buildchroot/source/demo/demo.lpmbuild"
            assert cmd[-1] == "/tmp/lpm-buildchroot/cache"
            built.write_text("package", encoding="utf-8")
            stdout = 'LPM_BUILDCHROOT_RESULT={"blob":"/tmp/lpm-buildchroot/cache/demo.lpm","splits":[]}\n'
            return chroot_helpers.subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(chroot_helpers, "mount_chroot_api", fake_mount_api)
    monkeypatch.setattr(chroot_helpers, "umount_chroot_api", fake_umount_api)
    monkeypatch.setattr(chroot_helpers.subprocess, "run", fake_run)

    args = Namespace(
        root=str(root),
        source=str(source),
        cache_dir=str(cache),
        output_dir=str(output),
        dry_run=False,
        verbose=False,
    )

    assert chroot_helpers.run_buildchroot(args) == 0
    assert mounted_api == [root]
    assert unmounted_api == [root]
    assert (output / "repo" / "demo.lpm").read_text(encoding="utf-8") == "package"
    assert [cmd[0] for cmd in commands] == ["mount", "mount", "chroot", "umount", "umount"]
