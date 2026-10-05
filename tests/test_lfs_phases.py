from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace

from lpm.lfs_phases import (
    _ensure_lfs_account,
    _finalize_lfs_temporary_layout,
    _prepare_lfs_layout,
    _prepare_section_source,
    _source_archive,
    _source_candidates,
    load_phase_plan,
    prepare_sources,
    run_phase_plan,
)


def test_tcl_source_archive_uses_upstream_src_spelling(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "tcl8.6.18-src.tar.gz"
    docs = sources / "tcl8.6.18-html.tar.gz"
    source.touch()
    docs.touch()

    assert _source_archive(sources, "ch-system-tcl") == source


def test_expect_source_archive_uses_upstream_compact_spelling(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "expect5.45.4.tar.gz"
    source.touch()

    assert _source_archive(sources, "ch-system-expect") == source


def test_libelf_section_uses_elfutils_source_archive(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "elfutils-0.194.tar.bz2"
    source.touch()

    assert _source_archive(sources, "ch-system-libelf") == source


def test_flit_core_section_uses_pypi_underscore_spelling(tmp_path: Path) -> None:
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "flit_core-3.12.0.tar.gz"
    source.touch()

    assert _source_archive(sources, "ch-system-flit-core") == source


def test_prepare_section_source_reuses_failed_tree_on_resume(tmp_path: Path) -> None:
    target = tmp_path / "root"
    sources = target / "sources"
    source_dir = sources / "glibc-2.44"
    source_dir.mkdir(parents=True)
    sentinel = source_dir / "build" / "libc.so"
    sentinel.parent.mkdir()
    sentinel.write_text("already built", encoding="utf-8")
    (source_dir / "README").write_text("existing source", encoding="utf-8")

    archive = sources / "glibc-2.44.tar.xz"
    seed = tmp_path / "seed" / "glibc-2.44"
    seed.mkdir(parents=True)
    (seed / "README").write_text("fresh", encoding="utf-8")
    with tarfile.open(archive, "w:xz") as stream:
        stream.add(seed, arcname="glibc-2.44")

    result = _prepare_section_source(
        target,
        "ch-system-glibc",
        "chroot",
        "lfs",
        reuse_existing=True,
    )

    assert result == source_dir
    assert sentinel.read_text(encoding="utf-8") == "already built"


def test_prepare_section_source_reextracts_incomplete_tree_on_resume(
    tmp_path: Path,
) -> None:
    target = tmp_path / "root"
    sources = target / "sources"
    source_dir = sources / "gcc-16.2.0"
    (source_dir / "build").mkdir(parents=True)

    archive = sources / "gcc-16.2.0.tar.xz"
    seed = tmp_path / "seed" / "gcc-16.2.0"
    seed.mkdir(parents=True)
    configure = seed / "configure"
    configure.write_text("#!/bin/sh\n", encoding="utf-8")
    with tarfile.open(archive, "w:xz") as stream:
        stream.add(seed, arcname="gcc-16.2.0")

    result = _prepare_section_source(
        target,
        "ch-system-gcc",
        "chroot",
        "lfs",
        reuse_existing=True,
    )

    assert result == source_dir
    assert (source_dir / "configure").is_file()
    assert not (source_dir / "build").exists()


def test_prepare_lfs_layout_creates_chapter_four_hierarchy(tmp_path: Path) -> None:
    target = tmp_path / "root"
    _prepare_lfs_layout(target)

    assert (target / "bin").readlink() == Path("usr/bin")
    assert (target / "lib").readlink() == Path("usr/lib")
    assert (target / "sbin").readlink() == Path("usr/sbin")
    assert (target / "tools").is_dir()
    assert (target / "var/lib").stat().st_mode & 0o7777 == 0o1777
    assert (target / "sources").stat().st_mode & 0o7777 == 0o1777


def test_finalize_lfs_temporary_layout_restores_var_lib_mode(tmp_path: Path) -> None:
    target = tmp_path / "root"
    _prepare_lfs_layout(target)

    _finalize_lfs_temporary_layout(target)

    assert (target / "var/lib").stat().st_mode & 0o7777 == 0o755


def test_ensure_lfs_account_creates_missing_locked_user(
    tmp_path: Path, monkeypatch
) -> None:
    import lpm.lfs_phases as phases

    _prepare_lfs_layout(tmp_path / "root")
    account = SimpleNamespace(pw_uid=1234, pw_gid=1234, pw_dir=str(tmp_path / "home"))
    lookups = iter((KeyError("missing"), account))

    def fake_getpwnam(_name):
        value = next(lookups)
        if isinstance(value, Exception):
            raise value
        return value

    commands: list[list[str]] = []
    monkeypatch.setattr(phases.pwd, "getpwnam", fake_getpwnam)
    monkeypatch.setattr(phases.grp, "getgrnam", lambda _name: (_ for _ in ()).throw(KeyError()))
    monkeypatch.setattr(phases.os, "geteuid", lambda: 0)
    owned: list[Path] = []
    monkeypatch.setattr(
        phases.os, "chown",
        lambda path, *_args, **_kwargs: owned.append(Path(path)),
    )
    monkeypatch.setattr(
        phases.subprocess, "run",
        lambda command, **_kwargs: commands.append(command) or SimpleNamespace(),
    )

    result = _ensure_lfs_account(tmp_path / "root", "lfs")
    assert result is account
    assert commands[0] == ["groupadd", "lfs"]
    assert commands[1][0] == "useradd"
    assert commands[2] == ["usermod", "--lock", "lfs"]
    if phases.os.uname().machine == "x86_64":
        assert tmp_path / "root" / "lib64" in owned
    assert tmp_path / "root" / "var/lib" not in owned


def test_ensure_default_build_account_prefers_systemd_sysusers(
    tmp_path: Path, monkeypatch
) -> None:
    import lpm.lfs_phases as phases

    target = tmp_path / "root"
    _prepare_lfs_layout(target)
    config = tmp_path / "lpm.conf"
    config.write_text("u lpm-build - - /var/lib/lpm-build /usr/bin/nologin\n")
    account = SimpleNamespace(
        pw_uid=1234,
        pw_gid=1234,
        pw_dir=str(tmp_path / "lpm-build"),
    )
    lookups = iter((KeyError("missing"), account))

    def fake_getpwnam(_name):
        value = next(lookups)
        if isinstance(value, Exception):
            raise value
        return value

    commands: list[list[str]] = []
    monkeypatch.setattr(phases, "LPM_SYSUSERS_CONFIG", config)
    monkeypatch.setattr(phases.pwd, "getpwnam", fake_getpwnam)
    monkeypatch.setattr(phases.shutil, "which", lambda name: "/usr/bin/systemd-sysusers")
    monkeypatch.setattr(phases.os, "geteuid", lambda: 0)
    monkeypatch.setattr(phases.os, "chown", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        phases.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(command) or SimpleNamespace(),
    )

    result = _ensure_lfs_account(target, "lpm-build")

    assert result is account
    assert commands == [["/usr/bin/systemd-sysusers", str(config)]]


def _plan(tmp_path: Path) -> Path:
    script = tmp_path / "section.sh"
    script.write_text("#!/bin/bash\necho ok\n", encoding="utf-8")
    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    plan = tmp_path / "phase-plan.json"
    plan.write_text(json.dumps({
        "format": "lpm-lfs-phase-plan", "format_version": 1,
        "book_sha256": "book", "sections": [{
            "number": 1, "section": "chapter08-test", "title": "8.1 Test",
            "phase": "final-system", "context": "host-root", "automatic": True,
            "script": str(script), "sha256": digest,
        }],
    }), encoding="utf-8")
    return plan


def test_phase_runner_checkpoints_success_and_resumes(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return object()

    plan = _plan(tmp_path)
    first = run_phase_plan(plan_path=plan, target=tmp_path / "root",
                           phases=("final-system",), force=True, run=fake_run)
    assert first["executed"] == ["chapter08-test"]
    second = run_phase_plan(plan_path=plan, target=tmp_path / "root",
                            phases=("final-system",), resume=True, force=True, run=fake_run)
    assert second["executed"] == []
    assert second["skipped"] == ["chapter08-test"]
    assert len(calls) == 1


def test_phase_runner_rejects_modified_script(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    payload = load_phase_plan(plan)
    Path(payload["sections"][0]["script"]).write_text("changed\n", encoding="utf-8")
    try:
        run_phase_plan(plan_path=plan, target=tmp_path / "root", dry_run=True, force=True)
    except RuntimeError as exc:
        assert "integrity check" in str(exc)
    else:
        raise AssertionError("modified section script was accepted")


def test_phase_runner_rejects_empty_selected_phase(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    try:
        run_phase_plan(
            plan_path=plan, target=tmp_path / "root",
            phases=("cross-toolchain",), force=True,
        )
    except RuntimeError as exc:
        assert "contains no sections" in str(exc)
    else:
        raise AssertionError("empty selected LFS phase was marked complete")


def test_prepare_sources_verifies_offline_cache(tmp_path: Path) -> None:
    import hashlib

    sources = tmp_path / "sources"
    sources.mkdir()
    archive = sources / "example.tar.xz"
    archive.write_bytes(b"example")
    digest = hashlib.md5(b"example").hexdigest()  # nosec B324
    wget = tmp_path / "wget-list"
    sums = tmp_path / "md5sums"
    wget.write_text("https://example.invalid/example.tar.xz\n", encoding="utf-8")
    sums.write_text(f"{digest}  example.tar.xz\n", encoding="utf-8")
    result = prepare_sources(
        wget_list=wget, md5sums=sums, destination=sources,
        version="13.1-systemd", offline=True
    )
    assert result["verified"] == ["example.tar.xz"]
    assert result["downloaded"] == []


def test_source_candidates_add_release_mirror_path() -> None:
    candidates = _source_candidates(
        "https://upstream.invalid/example.tar.xz",
        "example.tar.xz",
        "13.1-systemd",
        ("https://mirror.invalid/lfs-packages/",),
    )
    assert candidates == [
        "https://upstream.invalid/example.tar.xz",
        "https://mirror.invalid/lfs-packages/13.1/example.tar.xz",
    ]
