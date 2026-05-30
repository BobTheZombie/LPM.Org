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

    raw1 = (out1 / "build-manifest.json").read_text(encoding="utf-8")
    raw2 = (out2 / "build-manifest.json").read_text(encoding="utf-8")
    m1 = json.loads(raw1)
    m2 = json.loads(raw2)
    assert m1["package_order"] == ["b", "a"]

    def normalize_package_paths(packages: list[dict[str, object]], output_dir: Path) -> list[dict[str, object]]:
        normalized = []
        for pkg in packages:
            item = dict(pkg)
            for key in ("build_output_dir", "repo_dir", "planned_artifact"):
                item[key] = str(item[key]).replace(output_dir.as_posix(), "<output>")
            item["planned_artifacts"] = [
                str(path).replace(output_dir.as_posix(), "<output>")
                for path in item["planned_artifacts"]
            ]
            normalized.append(item)
        return normalized

    assert normalize_package_paths(m1["packages"], out1) == normalize_package_paths(m2["packages"], out2)
    assert raw1 == json.dumps(m1, indent=2, sort_keys=True) + "\n"
    assert raw2 == json.dumps(m2, indent=2, sort_keys=True) + "\n"


def test_buildgen_manifest_records_chroot_setup_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "packages"
    script = src / "demo.lpmbuild"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# demo\n", encoding="utf-8")

    from lpm import app as lpm_app

    def fake_capture(path: Path):
        assert path == script
        return {"NAME": "demo", "VERSION": "2", "RELEASE": "3", "ARCH": "x86_64"}, {"REQUIRES": []}, {}

    monkeypatch.setattr(lpm_app, "_capture_lpmbuild_metadata", fake_capture)

    root = tmp_path / "target-root"
    out = tmp_path / "out"
    args = Namespace(root=str(root), source=str(src), output_dir=str(out), dry_run=False, verbose=False)

    assert chroot_helpers.run_buildgen(args) == 0

    manifest = json.loads((out / "build-manifest.json").read_text(encoding="utf-8"))
    assert manifest["root"] == root.as_posix()
    assert manifest["output_dir"] == out.as_posix()
    assert manifest["repo_dir"] == (out / "repo").as_posix()
    assert manifest["bootstrap_packages"] == []
    assert manifest["chroot_setup"] == {
        "bootstrap_packages": [],
        "output_dir": out.as_posix(),
        "repo_dir": (out / "repo").as_posix(),
        "root": root.as_posix(),
    }
    assert manifest["packages"][0]["build_output_dir"] == (out / "build" / "demo").as_posix()
    assert manifest["packages"][0]["repo_dir"] == (out / "repo").as_posix()
    assert manifest["packages"][0]["planned_artifact"] == (out / "repo" / "demo-2-3.x86_64.zst").as_posix()
    assert manifest["packages"][0]["planned_artifacts"] == [(out / "repo" / "demo-2-3.x86_64.zst").as_posix()]


def test_buildgen_accepts_direct_lpmbuild_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "demo.lpmbuild"
    script.write_text("# demo\n", encoding="utf-8")

    from lpm import app as lpm_app

    def fake_capture(path: Path):
        assert path == script
        return {"NAME": "demo", "VERSION": "1"}, {"REQUIRES": []}, {}

    monkeypatch.setattr(lpm_app, "_capture_lpmbuild_metadata", fake_capture)

    out = tmp_path / "out"
    args = Namespace(root=str(tmp_path / "root"), source=str(script), output_dir=str(out), dry_run=False, verbose=False)

    assert chroot_helpers.run_buildgen(args) == 0

    manifest = json.loads((out / "build-manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"] == str(script)
    assert manifest["package_order"] == ["demo"]
    assert manifest["packages"][0]["script"] == str(script)


def test_buildgen_finds_lpmbuild_directly_in_source_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "packages"
    src.mkdir()
    script = src / "demo.lpmbuild"
    script.write_text("# demo\n", encoding="utf-8")

    from lpm import app as lpm_app

    def fake_capture(path: Path):
        assert path == script
        return {"NAME": "demo", "VERSION": "1"}, {"REQUIRES": []}, {}

    monkeypatch.setattr(lpm_app, "_capture_lpmbuild_metadata", fake_capture)

    out = tmp_path / "out"
    args = Namespace(root=str(tmp_path / "root"), source=str(src), output_dir=str(out), dry_run=False, verbose=False)

    assert chroot_helpers.run_buildgen(args) == 0

    manifest = json.loads((out / "build-manifest.json").read_text(encoding="utf-8"))
    assert manifest["package_order"] == ["demo"]
    assert manifest["packages"][0]["script"] == str(script)


def test_buildgen_finds_nested_maintainer_mode_layouts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "packages"
    script = src / "lpmbuilds" / "demo" / "1" / "demo.lpmbuild"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# demo\n", encoding="utf-8")

    from lpm import app as lpm_app

    def fake_capture(path: Path):
        assert path == script
        return {"NAME": "demo", "VERSION": "1"}, {"REQUIRES": []}, {}

    monkeypatch.setattr(lpm_app, "_capture_lpmbuild_metadata", fake_capture)

    out = tmp_path / "out"
    args = Namespace(root=str(tmp_path / "root"), source=str(src), output_dir=str(out), dry_run=False, verbose=False)

    assert chroot_helpers.run_buildgen(args) == 0

    manifest = json.loads((out / "build-manifest.json").read_text(encoding="utf-8"))
    assert manifest["package_order"] == ["demo"]
    assert manifest["packages"][0]["script"] == str(script)


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
    args = Namespace(
        root=str(tmp_path / "root"),
        source=str(src),
        output_dir=str(tmp_path / "out"),
        dry_run=False,
        verbose=False,
    )

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


def test_buildchroot_uses_manifest_chroot_setup_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "packages" / "demo" / "demo.lpmbuild"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# demo\n", encoding="utf-8")

    cli_out = tmp_path / "cli-out"
    cli_out.mkdir(parents=True, exist_ok=True)
    manifest_root = tmp_path / "manifest-root"
    manifest_out = tmp_path / "manifest-out"
    manifest_repo = tmp_path / "manifest-repo"
    manifest = {
        "root": manifest_root.as_posix(),
        "output_dir": manifest_out.as_posix(),
        "repo_dir": manifest_repo.as_posix(),
        "bootstrap_packages": ["base", "toolchain"],
        "package_order": ["demo"],
        "packages": [{"name": "demo", "script": script.as_posix(), "depends": []}],
    }
    (cli_out / "build-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    built_blob = tmp_path / "cache" / "demo-1-any.zst"

    from lpm import app as lpm_app

    def fake_run_lpmbuild(*_args, **_kwargs):
        built_blob.parent.mkdir(parents=True, exist_ok=True)
        built_blob.write_text("blob", encoding="utf-8")
        return built_blob, 0.0, built_blob.stat().st_size, []

    install_calls: list[tuple[Path, list[str]]] = []

    def fake_run_root_install(root: Path, packages: list[str], *, dry_run: bool = False):
        install_calls.append((root, list(packages)))
        return {"returncode": 0, "dry_run": dry_run}

    monkeypatch.setattr(lpm_app, "run_lpmbuild", fake_run_lpmbuild)
    monkeypatch.setattr(chroot_helpers, "_run_root_install", fake_run_root_install)

    args = Namespace(
        root=str(tmp_path / "cli-root"),
        source=str(tmp_path / "packages"),
        cache_dir=str(tmp_path / "cache"),
        output_dir=str(cli_out),
        dry_run=False,
        verbose=False,
    )

    rc = chroot_helpers.run_buildchroot(args)
    assert rc == 0
    assert install_calls == [(manifest_root, ["base", "toolchain"]), (manifest_root, ["demo"])]
    assert (manifest_repo / built_blob.name).exists()
    assert not (cli_out / "repo" / built_blob.name).exists()


def test_buildchroot_installs_built_packages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = tmp_path / "packages" / "demo" / "demo.lpmbuild"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# demo\n", encoding="utf-8")

    out = tmp_path / "out"
    out.mkdir(parents=True, exist_ok=True)
    manifest = {
        "package_order": ["demo"],
        "packages": [{"name": "demo", "script": str(script), "depends": []}],
    }
    (out / "build-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    built_blob = tmp_path / "cache" / "demo-1-any.zst"

    from lpm import app as lpm_app

    def fake_run_lpmbuild(*_args, **_kwargs):
        built_blob.parent.mkdir(parents=True, exist_ok=True)
        built_blob.write_text("blob", encoding="utf-8")
        return built_blob, 0.0, built_blob.stat().st_size, []

    install_calls: list[list[str]] = []

    def fake_run_root_install(_root: Path, packages: list[str], *, dry_run: bool = False):
        install_calls.append(list(packages))
        return {"returncode": 0, "dry_run": dry_run}

    monkeypatch.setattr(lpm_app, "run_lpmbuild", fake_run_lpmbuild)
    monkeypatch.setattr(chroot_helpers, "_run_root_install", fake_run_root_install)

    args = Namespace(
        root=str(tmp_path / "root"),
        source=str(tmp_path / "packages"),
        cache_dir=str(tmp_path / "cache"),
        output_dir=str(out),
        dry_run=False,
        verbose=False,
    )
    rc = chroot_helpers.run_buildchroot(args)
    assert rc == 0
    assert install_calls == [["demo"]]
    assert (out / "repo" / built_blob.name).exists()
