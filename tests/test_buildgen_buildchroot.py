from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest

from lpm import chroot_helpers


def test_buildgen_manifest_is_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    args1 = Namespace(
        root=str(tmp_path / "root"),
        source=str(src),
        output_dir=str(out1),
        dry_run=False,
        verbose=False,
    )
    args2 = Namespace(
        root=str(tmp_path / "root"),
        source=str(src),
        output_dir=str(out2),
        dry_run=False,
        verbose=False,
    )

    assert chroot_helpers.run_buildgen(args1) == 0
    assert chroot_helpers.run_buildgen(args2) == 0

    raw1 = (out1 / "build-manifest.json").read_text(encoding="utf-8")
    raw2 = (out2 / "build-manifest.json").read_text(encoding="utf-8")
    m1 = json.loads(raw1)
    m2 = json.loads(raw2)
    assert m1["package_order"] == ["b", "a"]

    def normalize_package_paths(
        packages: list[dict[str, object]], output_dir: Path
    ) -> list[dict[str, object]]:
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

    assert normalize_package_paths(m1["packages"], out1) == normalize_package_paths(
        m2["packages"], out2
    )
    assert raw1 == json.dumps(m1, indent=2, sort_keys=True) + "\n"
    assert raw2 == json.dumps(m2, indent=2, sort_keys=True) + "\n"


def test_buildgen_manifest_records_chroot_setup_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "packages"
    script = src / "demo.lpmbuild"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# demo\n", encoding="utf-8")

    from lpm import app as lpm_app

    def fake_capture(path: Path):
        assert path == script
        return (
            {"NAME": "demo", "VERSION": "2", "RELEASE": "3", "ARCH": "x86_64"},
            {"REQUIRES": []},
            {},
        )

    monkeypatch.setattr(lpm_app, "_capture_lpmbuild_metadata", fake_capture)

    root = tmp_path / "target-root"
    out = tmp_path / "out"
    args = Namespace(
        root=str(root),
        source=str(src),
        output_dir=str(out),
        dry_run=False,
        verbose=False,
    )

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
    assert (
        manifest["packages"][0]["build_output_dir"]
        == (out / "build" / "demo").as_posix()
    )
    assert manifest["packages"][0]["repo_dir"] == (out / "repo").as_posix()
    assert (
        manifest["packages"][0]["planned_artifact"]
        == (out / "repo" / "demo-2-3.x86_64.zst").as_posix()
    )
    assert manifest["packages"][0]["planned_artifacts"] == [
        (out / "repo" / "demo-2-3.x86_64.zst").as_posix()
    ]


def test_buildgen_accepts_direct_lpmbuild_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "demo.lpmbuild"
    script.write_text("# demo\n", encoding="utf-8")

    from lpm import app as lpm_app

    def fake_capture(path: Path):
        assert path == script
        return {"NAME": "demo", "VERSION": "1"}, {"REQUIRES": []}, {}

    monkeypatch.setattr(lpm_app, "_capture_lpmbuild_metadata", fake_capture)

    out = tmp_path / "out"
    args = Namespace(
        root=str(tmp_path / "root"),
        source=str(script),
        output_dir=str(out),
        dry_run=False,
        verbose=False,
    )

    assert chroot_helpers.run_buildgen(args) == 0

    manifest = json.loads((out / "build-manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"] == str(script)
    assert manifest["package_order"] == ["demo"]
    assert manifest["packages"][0]["script"] == str(script)


def test_buildgen_finds_lpmbuild_directly_in_source_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    args = Namespace(
        root=str(tmp_path / "root"),
        source=str(src),
        output_dir=str(out),
        dry_run=False,
        verbose=False,
    )

    assert chroot_helpers.run_buildgen(args) == 0

    manifest = json.loads((out / "build-manifest.json").read_text(encoding="utf-8"))
    assert manifest["package_order"] == ["demo"]
    assert manifest["packages"][0]["script"] == str(script)


def test_buildgen_finds_nested_maintainer_mode_layouts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    args = Namespace(
        root=str(tmp_path / "root"),
        source=str(src),
        output_dir=str(out),
        dry_run=False,
        verbose=False,
    )

    assert chroot_helpers.run_buildgen(args) == 0

    manifest = json.loads((out / "build-manifest.json").read_text(encoding="utf-8"))
    assert manifest["package_order"] == ["demo"]
    assert manifest["packages"][0]["script"] == str(script)


def test_buildgen_resolves_virtual_dependencies_from_provides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "packages"
    for name in ("consumer", "zlib-ng"):
        script = src / name / f"{name}.lpmbuild"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("# stub\n", encoding="utf-8")

    from lpm import app as lpm_app

    def fake_capture(path: Path):
        if path.stem == "consumer":
            return (
                {"NAME": "consumer", "VERSION": "1"},
                {"REQUIRES": ["zlib >= 1", "compression-lib"]},
                {},
            )
        return (
            {"NAME": "zlib-ng", "VERSION": "1"},
            {"REQUIRES": [], "PROVIDES": ["compression-lib"]},
            {"META_PROVIDES": {"zlib-ng": ["zlib"]}},
        )

    monkeypatch.setattr(lpm_app, "_capture_lpmbuild_metadata", fake_capture)

    out = tmp_path / "out"
    args = Namespace(
        root=str(tmp_path / "root"),
        source=str(src),
        output_dir=str(out),
        dry_run=False,
        verbose=False,
    )

    assert chroot_helpers.run_buildgen(args) == 0

    manifest = json.loads((out / "build-manifest.json").read_text(encoding="utf-8"))
    assert manifest["package_order"] == ["zlib-ng", "consumer"]
    packages = {pkg["name"]: pkg for pkg in manifest["packages"]}
    assert packages["consumer"]["depends"] == ["zlib-ng"]
    assert packages["zlib-ng"]["provides"] == ["compression-lib", "zlib"]


def test_buildgen_cycle_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        "packages": [
            {
                "name": "demo",
                "script": str(tmp_path / "missing.lpmbuild"),
                "depends": [],
            }
        ],
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


def test_buildchroot_uses_manifest_chroot_setup_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    install_calls: list[tuple[Path, list[str]]] = []
    local_install_calls: list[tuple[Path, list[Path]]] = []

    def fake_mount(root: Path, state: chroot_helpers.ChrootMountState):
        state.mark_mounted("proc")
        return state

    def fake_umount(_root: Path, state: chroot_helpers.ChrootMountState):
        return state

    def fake_run_chroot_build(root: Path, _script: Path, outdir: Path) -> int:
        assert root == manifest_root
        built_blob = outdir / "demo-1-any.zst"
        built_blob.parent.mkdir(parents=True, exist_ok=True)
        built_blob.write_text("blob", encoding="utf-8")
        return 0

    def fake_run_root_install(
        root: Path, packages: list[str], *, dry_run: bool = False
    ):
        install_calls.append((root, list(packages)))
        return {"returncode": 0, "dry_run": dry_run}

    def fake_run_root_install_local(
        root: Path, artifacts: list[Path], *, dry_run: bool = False
    ):
        local_install_calls.append((root, list(artifacts)))
        return {"returncode": 0, "dry_run": dry_run}

    monkeypatch.setattr(chroot_helpers, "mount_chroot_api", fake_mount)
    monkeypatch.setattr(chroot_helpers, "umount_chroot_api", fake_umount)
    monkeypatch.setattr(chroot_helpers, "_run_chroot_build", fake_run_chroot_build)
    monkeypatch.setattr(chroot_helpers, "_run_root_install", fake_run_root_install)
    monkeypatch.setattr(
        chroot_helpers, "_run_root_install_local", fake_run_root_install_local
    )

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
    assert install_calls == [(manifest_root, ["base", "toolchain"])]
    assert local_install_calls == [
        (manifest_root, [manifest_out / "repo" / "demo-1-any.zst"])
    ]
    assert (manifest_out / "repo" / "demo-1-any.zst").exists()
    assert not (manifest_repo / "demo-1-any.zst").exists()


def test_buildchroot_chroot_command_targets_requested_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    script = root / "var/lib/lpm/buildchroot/inputs/0001-demo/demo.lpmbuild"
    outdir = root / "var/cache/lpm/buildchroot"
    script.parent.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)
    script.write_text("# demo\n", encoding="utf-8")

    generated: list[tuple[Path, list[str]]] = []
    ran: list[list[str]] = []

    class Proc:
        returncode = 0

    def fake_generate_chroot_command(target: Path, command: list[str]) -> list[str]:
        generated.append((target, list(command)))
        return ["chroot", str(target), *command]

    def fake_run(cmd: list[str], check: bool = False):
        ran.append(list(cmd))
        return Proc()

    monkeypatch.setattr(
        chroot_helpers, "generate_chroot_command", fake_generate_chroot_command
    )
    monkeypatch.setattr(chroot_helpers.subprocess, "run", fake_run)

    assert chroot_helpers._run_chroot_build(root, script, outdir) == 0
    assert generated == [
        (
            root,
            [
                "lpm",
                "buildpkg",
                "/var/lib/lpm/buildchroot/inputs/0001-demo/demo.lpmbuild",
                "--outdir",
                "/var/cache/lpm/buildchroot",
                "--install-default",
                "n",
            ],
        )
    ]
    assert ran == [["chroot", str(root), *generated[0][1]]]


def test_buildchroot_unmounts_api_filesystems_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    events: list[tuple[str, Path, list[str]]] = []

    def fake_mount(root: Path, state: chroot_helpers.ChrootMountState):
        state.mark_mounted("proc")
        events.append(("mount", root, list(state.mounted)))
        return state

    def fake_umount(root: Path, state: chroot_helpers.ChrootMountState):
        events.append(("umount", root, list(state.mounted)))
        return state

    def fake_run_chroot_build(_root: Path, _script: Path, outdir: Path) -> int:
        (outdir / "demo-1-any.zst").write_text("blob", encoding="utf-8")
        return 0

    monkeypatch.setattr(chroot_helpers, "mount_chroot_api", fake_mount)
    monkeypatch.setattr(chroot_helpers, "umount_chroot_api", fake_umount)
    monkeypatch.setattr(chroot_helpers, "_run_chroot_build", fake_run_chroot_build)
    monkeypatch.setattr(
        chroot_helpers,
        "_run_root_install_local",
        lambda *_args, **_kwargs: {"returncode": 0},
    )

    args = Namespace(
        root=str(tmp_path / "root"),
        source=str(tmp_path / "packages"),
        cache_dir=str(tmp_path / "cache"),
        output_dir=str(out),
        dry_run=False,
        verbose=False,
    )
    assert chroot_helpers.run_buildchroot(args) == 0
    assert events == [
        ("mount", tmp_path / "root", ["proc"]),
        ("umount", tmp_path / "root", ["proc"]),
    ]


def test_buildchroot_unmounts_api_filesystems_on_build_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    events: list[str] = []

    def fake_mount(root: Path, state: chroot_helpers.ChrootMountState):
        state.mark_mounted("proc")
        events.append(f"mount:{root}")
        return state

    def fake_umount(root: Path, state: chroot_helpers.ChrootMountState):
        events.append(f"umount:{root}:{','.join(state.mounted)}")
        return state

    monkeypatch.setattr(chroot_helpers, "mount_chroot_api", fake_mount)
    monkeypatch.setattr(chroot_helpers, "umount_chroot_api", fake_umount)
    monkeypatch.setattr(
        chroot_helpers, "_run_chroot_build", lambda *_args, **_kwargs: 77
    )

    args = Namespace(
        root=str(tmp_path / "root"),
        source=str(tmp_path / "packages"),
        cache_dir=str(tmp_path / "cache"),
        output_dir=str(out),
        dry_run=False,
        verbose=False,
    )
    assert chroot_helpers.run_buildchroot(args) == 77
    assert events == [f"mount:{tmp_path / 'root'}", f"umount:{tmp_path / 'root'}:proc"]


def test_buildchroot_installs_built_local_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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

    installed_artifacts: list[Path] = []

    def fake_mount(root: Path, state: chroot_helpers.ChrootMountState):
        return state

    def fake_umount(_root: Path, state: chroot_helpers.ChrootMountState):
        return state

    def fake_run_chroot_build(_root: Path, _script: Path, outdir: Path) -> int:
        built_blob = outdir / "demo-1-any.zst"
        built_blob.write_text("blob", encoding="utf-8")
        return 0

    def fake_run_root_install_local(
        _root: Path, artifacts: list[Path], *, dry_run: bool = False
    ):
        installed_artifacts.extend(artifacts)
        return {"returncode": 0, "dry_run": dry_run}

    monkeypatch.setattr(chroot_helpers, "mount_chroot_api", fake_mount)
    monkeypatch.setattr(chroot_helpers, "umount_chroot_api", fake_umount)
    monkeypatch.setattr(chroot_helpers, "_run_chroot_build", fake_run_chroot_build)
    monkeypatch.setattr(
        chroot_helpers, "_run_root_install_local", fake_run_root_install_local
    )

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
    assert installed_artifacts == [out / "repo" / "demo-1-any.zst"]
    assert (out / "repo" / "demo-1-any.zst").exists()
