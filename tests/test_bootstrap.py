import json
import os
import sqlite3
import sys
import time
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.lpm.app import (  # noqa: E402
    PkgMeta,
    Universe,
    cmd_bootstrap,
    db,
    BootstrapRuleSet,
)


def test_bootstrap_build_injects_local_provider(tmp_path, monkeypatch):
    script = tmp_path / "system-base.lpmbuild"
    script.write_text(
        "\n".join(
            [
                "NAME=system-base",
                "VERSION=1.0.0",
                "RELEASE=1",
                "ARCH=x86_64",
                'SUMMARY="System base"',
                "provides=(\"glibc\")",
                "REQUIRES=(\"foo\")",
            ]
        )
    )

    built_pkg_path = tmp_path / "system-base-1.0.0-1.x86_64.lpm"

    base_pkg = PkgMeta(name="lpm-base", version="1.0.0")
    core_pkg = PkgMeta(name="lpm-core", version="1.0.0")
    foo_pkg = PkgMeta(name="foo", version="1.0.0")

    def fake_build_universe():
        universe = Universe(candidates_by_name={}, providers={}, installed={}, pins={}, holds=set())
        for meta in (base_pkg, core_pkg, foo_pkg):
            universe.candidates_by_name.setdefault(meta.name, []).append(meta)
            universe.providers.setdefault(meta.name, []).append(meta)
        return universe

    observed = {}

    def fake_solve(goals, universe):
        observed["goals"] = goals
        observed["universe"] = universe
        system_base_candidates = universe.candidates_by_name.get("system-base")
        assert system_base_candidates, "bootstrap build candidate missing from universe"
        return [base_pkg, core_pkg, system_base_candidates[0]]

    def fake_run_lpmbuild(*args, **kwargs):
        return built_pkg_path, 0.0, 0, []

    def fake_do_install(plan, root, dry, verify, force, explicit, allow_fallback, force_build, local_overrides):
        assert Path(root).exists()
        assert any(pkg.name == "system-base" for pkg in plan)
        assert local_overrides["system-base"] == built_pkg_path

    monkeypatch.setattr("src.lpm.app._load_mkchroot_rules", lambda path=...: BootstrapRuleSet())
    monkeypatch.setattr("src.lpm.app.build_universe", fake_build_universe)
    monkeypatch.setattr("src.lpm.app.solve", fake_solve)
    monkeypatch.setattr("src.lpm.app.run_lpmbuild", fake_run_lpmbuild)
    monkeypatch.setattr("src.lpm.app.do_install", fake_do_install)

    args = SimpleNamespace(
        root=str(tmp_path / "root"),
        include=[],
        no_verify=True,
        build=str(script),
    )

    cmd_bootstrap(args)

    assert observed["goals"] == ["lpm-base", "lpm-core", "system-base"]
    system_base_candidates = observed["universe"].candidates_by_name["system-base"]
    assert system_base_candidates[0].repo == "(bootstrap)"
    assert system_base_candidates[0].requires == ["foo"]
    assert system_base_candidates[0].provides == ["glibc"]


def test_bootstrap_build_uses_isolated_db(tmp_path, monkeypatch):
    script = tmp_path / "system-base.lpmbuild"
    script.write_text(
        "\n".join(
            [
                "NAME=system-base",
                "VERSION=1.0.0",
                "RELEASE=1",
                "ARCH=x86_64",
                'SUMMARY="System base"',
                'PROVIDES=("virtual-system")',
                "REQUIRES=(\"foo\")",
            ]
        )
    )

    built_pkg_path = tmp_path / "system-base-1.0.0-1.x86_64.lpm"

    base_pkg = PkgMeta(name="lpm-base", version="1.0.0")
    core_pkg = PkgMeta(name="lpm-core", version="1.0.0")
    foo_pkg = PkgMeta(name="foo", version="1.0.0")

    def fake_build_universe():
        universe = Universe(candidates_by_name={}, providers={}, installed={}, pins={}, holds=set())
        for meta in (base_pkg, core_pkg, foo_pkg):
            universe.candidates_by_name.setdefault(meta.name, []).append(meta)
            universe.providers.setdefault(meta.name, []).append(meta)
        return universe

    def fake_solve(goals, universe):
        return [base_pkg, core_pkg, PkgMeta(name="system-base", version="1.0.0")]

    def fake_run_lpmbuild(*args, **kwargs):
        return built_pkg_path, 0.0, 0, []

    def fake_do_install(
        plan,
        root,
        dry,
        verify,
        force,
        explicit,
        allow_fallback,
        force_build,
        local_overrides,
    ):
        assert Path(root).exists()
        conn = db()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO installed(
                    name, version, release, arch, provides, symbols, requires, manifest, explicit, install_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "system-base",
                    "1.0.0",
                    "1",
                    "x86_64",
                    json.dumps(["virtual-system"]),
                    "[]",
                    json.dumps(["foo"]),
                    json.dumps([]),
                    1,
                    int(time.time()),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    monkeypatch.setattr("src.lpm.app._load_mkchroot_rules", lambda path=...: BootstrapRuleSet())
    monkeypatch.setattr("src.lpm.app.build_universe", fake_build_universe)
    monkeypatch.setattr("src.lpm.app.solve", fake_solve)
    monkeypatch.setattr("src.lpm.app.run_lpmbuild", fake_run_lpmbuild)
    monkeypatch.setattr("src.lpm.app.do_install", fake_do_install)

    root = tmp_path / "root"
    args = SimpleNamespace(
        root=str(root),
        include=[],
        no_verify=True,
        build=str(script),
    )

    cmd_bootstrap(args)

    db_path = root / "var" / "lib" / "lpm" / "state.db"
    assert db_path.exists()

    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT provides FROM installed WHERE name=?", ("system-base",)
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    provides = json.loads(row[0])
    assert "virtual-system" in provides


def test_bootstrap_force_build_builds_all_plan_packages(tmp_path, monkeypatch):
    rules = BootstrapRuleSet()
    rules.base = ["pkg-alpha", "pkg-beta"]

    plan = [
        PkgMeta(name="pkg-alpha", version="1.0.0"),
        PkgMeta(name="pkg-beta", version="1.0.0"),
    ]

    def fake_build_universe():
        universe = Universe(candidates_by_name={}, providers={}, installed={}, pins={}, holds=set())
        for meta in plan:
            universe.candidates_by_name.setdefault(meta.name, []).append(meta)
            universe.providers.setdefault(meta.name, []).append(meta)
        return universe

    def fake_solve(goals, universe):
        assert set(goals) == {"pkg-alpha", "pkg-beta"}
        return plan

    fetch_calls = []
    fetch_targets = {}

    def fake_fetch_lpmbuild(name, dest):
        fetch_calls.append(name)
        fetch_targets[Path(dest)] = name
        Path(dest).write_text("# dummy script")
        return dest

    def fake_run_lpmbuild(script_path, *args, **kwargs):
        script_path = Path(script_path)
        name = fetch_targets.get(script_path)
        assert name is not None, f"unexpected lpmbuild path: {script_path}"
        built_path = tmp_path / f"{name}.lpm"
        built_path.write_text("package")
        return built_path, 0.0, 0, []

    def fake_do_install(
        plan,
        root,
        dry,
        verify,
        force,
        explicit,
        allow_fallback,
        force_build,
        local_overrides,
    ):
        assert force_build is True
        assert Path(root).exists()
        expected = {pkg.name: tmp_path / f"{pkg.name}.lpm" for pkg in plan}
        assert local_overrides == expected

    monkeypatch.setattr("src.lpm.app._load_mkchroot_rules", lambda path=...: rules)
    monkeypatch.setattr("src.lpm.app.build_universe", fake_build_universe)
    monkeypatch.setattr("src.lpm.app.solve", fake_solve)
    monkeypatch.setattr("src.lpm.app.fetch_lpmbuild", fake_fetch_lpmbuild)
    monkeypatch.setattr("src.lpm.app.run_lpmbuild", fake_run_lpmbuild)
    monkeypatch.setattr("src.lpm.app.do_install", fake_do_install)

    args = SimpleNamespace(
        root=str(tmp_path / "root"),
        include=[],
        no_verify=True,
        build=True,
    )

    cmd_bootstrap(args)
    assert set(fetch_calls) == {"pkg-alpha", "pkg-beta"}


def test_bootstrap_respects_custom_base_override(tmp_path, monkeypatch):
    rules = BootstrapRuleSet()
    rules.base = ["custom-base"]
    rules.include = []
    rules.build = []

    custom_pkg = PkgMeta(name="custom-base", version="1.0.0")

    def fake_build_universe():
        universe = Universe(candidates_by_name={}, providers={}, installed={}, pins={}, holds=set())
        universe.candidates_by_name.setdefault(custom_pkg.name, []).append(custom_pkg)
        universe.providers.setdefault(custom_pkg.name, []).append(custom_pkg)
        return universe

    def fake_solve(goals, universe):
        assert goals == ["custom-base"]
        return [custom_pkg]

    def fake_do_install(
        plan,
        root,
        dry,
        verify,
        force,
        explicit,
        allow_fallback,
        force_build,
        local_overrides,
    ):
        assert [pkg.name for pkg in plan] == ["custom-base"]

    monkeypatch.setattr("src.lpm.app._load_mkchroot_rules", lambda path=...: rules)
    monkeypatch.setattr("src.lpm.app.build_universe", fake_build_universe)
    monkeypatch.setattr("src.lpm.app.solve", fake_solve)
    monkeypatch.setattr("src.lpm.app.do_install", fake_do_install)

    args = SimpleNamespace(
        root=str(tmp_path / "root"),
        include=[],
        no_verify=True,
        build=False,
    )

    cmd_bootstrap(args)


def test_bootstrap_respects_custom_core_override(tmp_path, monkeypatch):
    rules = BootstrapRuleSet()
    rules.base = ["lpm-base"]
    rules.include = []
    rules.build = []

    base_pkg = PkgMeta(name="lpm-base", version="1.0.0")

    def fake_build_universe():
        universe = Universe(candidates_by_name={}, providers={}, installed={}, pins={}, holds=set())
        universe.candidates_by_name.setdefault(base_pkg.name, []).append(base_pkg)
        universe.providers.setdefault(base_pkg.name, []).append(base_pkg)
        return universe

    def fake_solve(goals, universe):
        assert goals == ["lpm-base"]
        return [base_pkg]

    def fake_do_install(
        plan,
        root,
        dry,
        verify,
        force,
        explicit,
        allow_fallback,
        force_build,
        local_overrides,
    ):
        assert [pkg.name for pkg in plan] == ["lpm-base"]

    monkeypatch.setattr("src.lpm.app._load_mkchroot_rules", lambda path=...: rules)
    monkeypatch.setattr("src.lpm.app.build_universe", fake_build_universe)
    monkeypatch.setattr("src.lpm.app.solve", fake_solve)
    monkeypatch.setattr("src.lpm.app.do_install", fake_do_install)

    args = SimpleNamespace(
        root=str(tmp_path / "root"),
        include=[],
        no_verify=True,
        build=False,
    )

    cmd_bootstrap(args)


def test_bootstrap_build_passes_dependency_overrides(tmp_path, monkeypatch):
    script = tmp_path / "system-base.lpmbuild"
    script.write_text(
        "\n".join(
            [
                "NAME=system-base",
                "VERSION=1.0.0",
                "RELEASE=1",
                "ARCH=x86_64",
                'SUMMARY="System base"',
                'REQUIRES=("foo")',
            ]
        )
    )

    built_pkg_path = tmp_path / "system-base-1.0.0-1.x86_64.lpm"
    dep_pkg_path = tmp_path / "foo-1.0.0-1.x86_64.lpm"

    base_pkg = PkgMeta(name="lpm-base", version="1.0.0")
    core_pkg = PkgMeta(name="lpm-core", version="1.0.0")
    foo_pkg = PkgMeta(name="foo", version="1.0.0")

    def fake_build_universe():
        universe = Universe(candidates_by_name={}, providers={}, installed={}, pins={}, holds=set())
        for meta in (base_pkg, core_pkg, foo_pkg):
            universe.candidates_by_name.setdefault(meta.name, []).append(meta)
            universe.providers.setdefault(meta.name, []).append(meta)
        return universe

    def fake_solve(goals, universe):
        return [base_pkg, core_pkg, foo_pkg, PkgMeta(name="system-base", version="1.0.0")]

    def fake_run_lpmbuild(script_path, *args, **kwargs):
        on_built = kwargs.get("on_built_package")
        if on_built:
            on_built(built_pkg_path, PkgMeta(name="system-base", version="1.0.0"))
            on_built(dep_pkg_path, PkgMeta(name="foo", version="1.0.0"))
        return built_pkg_path, 0.0, 0, []

    observed_overrides = {}

    def fake_do_install(
        plan,
        root,
        dry,
        verify,
        force,
        explicit,
        allow_fallback,
        force_build,
        local_overrides,
    ):
        observed_overrides.update(local_overrides)

    rules = BootstrapRuleSet()
    rules.base = []
    rules.include = []
    rules.build = []

    monkeypatch.setattr("src.lpm.app._load_mkchroot_rules", lambda path=...: rules)
    monkeypatch.setattr("src.lpm.app.build_universe", fake_build_universe)
    monkeypatch.setattr("src.lpm.app.solve", fake_solve)
    monkeypatch.setattr("src.lpm.app.run_lpmbuild", fake_run_lpmbuild)
    monkeypatch.setattr("src.lpm.app.do_install", fake_do_install)

    args = SimpleNamespace(
        root=str(tmp_path / "root"),
        include=[],
        no_verify=True,
        build=str(script),
    )

    cmd_bootstrap(args)

    assert observed_overrides["system-base"] == built_pkg_path
    assert observed_overrides["foo"] == dep_pkg_path


def test_bootstrap_build_creates_fhs_structure(tmp_path, monkeypatch):
    rules = BootstrapRuleSet()
    rules.base = []
    rules.include = []
    rules.build = []

    def fake_build_universe():
        return Universe(candidates_by_name={}, providers={}, installed={}, pins={}, holds=set())

    def fake_solve(goals, universe):
        return []

    monkeypatch.setattr("src.lpm.app._load_mkchroot_rules", lambda path=...: rules)
    monkeypatch.setattr("src.lpm.app.build_universe", fake_build_universe)
    monkeypatch.setattr("src.lpm.app.solve", fake_solve)
    monkeypatch.setattr("src.lpm.app.do_install", lambda *args, **kwargs: None)

    root = tmp_path / "root"

    args = SimpleNamespace(
        root=str(root),
        include=[],
        no_verify=True,
        build=True,
    )

    cmd_bootstrap(args)

    expected_dirs = {
        "dev",
        "proc",
        "sys",
        "tmp",
        "var",
        "etc",
        "bin",
        "sbin",
        "lib",
        "lib64",
        "opt",
        "home",
        "run",
        "usr",
        "usr/bin",
        "usr/sbin",
        "usr/lib",
        "usr/lib64",
        "usr/local",
        "usr/local/bin",
        "usr/local/sbin",
        "usr/local/lib",
        "usr/share",
        "var/cache",
        "var/lib",
        "var/log",
        "var/spool",
        "var/tmp",
        "var/run",
    }

    for rel in expected_dirs:
        assert (root / rel).is_dir(), f"missing {rel}"
