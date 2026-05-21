from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from lpm import app as lpm


def _prepare_db(monkeypatch, tmp_path, rows):
    db_path = tmp_path / "rebuild.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE installed (name TEXT, requires TEXT)")
    conn.executemany("INSERT INTO installed(name, requires) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    monkeypatch.setattr(lpm, "db", lambda: sqlite3.connect(db_path))
    return db_path


def test_rebuild_computes_transitive_closure_and_order(monkeypatch, tmp_path):
    _prepare_db(
        monkeypatch,
        tmp_path,
        [
            ("libxml2", json.dumps([])),
            ("a", json.dumps(["libxml2"])),
            ("b", json.dumps(["a"])),
            ("c", json.dumps(["libxml2"])),
        ],
    )
    for pkg in ("libxml2", "a", "b", "c"):
        p = tmp_path / "packages" / pkg
        p.mkdir(parents=True)
        (p / f"{pkg}.lpmbuild").write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    called = []

    def fake_run(script, outdir, **kwargs):
        called.append((Path(script), Path(outdir), kwargs))
        return (tmp_path / "dummy.lpm", 0.0, 1, [])

    monkeypatch.setattr(lpm, "run_lpmbuild", fake_run)
    args = lpm.build_parser().parse_args(["rebuild", "libxml2", "--outdir", str(tmp_path)])
    lpm.cmd_rebuild(args)

    order = [item[0].parent.name for item in called]
    assert order == ["libxml2", "a", "c", "b"]


def test_rebuild_missing_script_fails_with_clear_error(monkeypatch, tmp_path):
    _prepare_db(
        monkeypatch,
        tmp_path,
        [
            ("libxml2", json.dumps([])),
            ("a", json.dumps(["libxml2"])),
        ],
    )
    p = tmp_path / "packages" / "libxml2"
    p.mkdir(parents=True)
    (p / "libxml2.lpmbuild").write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    args = lpm.build_parser().parse_args(["rebuild", "libxml2"])
    with pytest.raises(SystemExit):
        lpm.cmd_rebuild(args)


def test_rebuild_parser_wiring_and_upgradepkg_alias_intact():
    parser = lpm.build_parser()

    rebuild = parser.parse_args(["rebuild", "libxml2", "--no-deps", "--install-default", "n"])
    assert rebuild.func is lpm.cmd_rebuild
    assert rebuild.name == "libxml2"
    assert rebuild.no_deps is True
    assert rebuild.install_default == "n"
    assert rebuild.cycle_policy == "fail"

    upgrade_alias = parser.parse_args(["upgradepkg", "demo", "--no-delta"])
    assert upgrade_alias.func is lpm.cmd_upgrade
    assert upgrade_alias.names == ["demo"]


def test_rebuild_order_is_deterministic(monkeypatch, tmp_path):
    rows = [
        ("libxml2", json.dumps([])),
        ("zeta", json.dumps(["libxml2"])),
        ("alpha", json.dumps(["libxml2"])),
    ]
    _prepare_db(monkeypatch, tmp_path, rows)
    for pkg in ("libxml2", "alpha", "zeta"):
        p = tmp_path / "packages" / pkg
        p.mkdir(parents=True)
        (p / f"{pkg}.lpmbuild").write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)

    observed = []

    def fake_run(script, _outdir, **_kwargs):
        observed.append(Path(script).parent.name)
        return (tmp_path / "dummy.lpm", 0.0, 1, [])

    monkeypatch.setattr(lpm, "run_lpmbuild", fake_run)
    args = lpm.build_parser().parse_args(["rebuild", "libxml2"])
    lpm.cmd_rebuild(args)
    first = list(observed)
    observed.clear()
    lpm.cmd_rebuild(args)
    second = list(observed)

    assert first == ["libxml2", "alpha", "zeta"]
    assert second == first


def test_rebuild_cycle_group_is_deterministic_with_policy_group(monkeypatch, tmp_path, capsys):
    _prepare_db(
        monkeypatch,
        tmp_path,
        [
            ("base", json.dumps([])),
            ("alpha", json.dumps(["base", "beta"])),
            ("beta", json.dumps(["base", "gamma"])),
            ("gamma", json.dumps(["beta"])),
        ],
    )
    for pkg in ("base", "alpha", "beta", "gamma"):
        p = tmp_path / "packages" / pkg
        p.mkdir(parents=True)
        (p / f"{pkg}.lpmbuild").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    calls = []

    def fake_run(script, _outdir, **_kwargs):
        calls.append(Path(script).parent.name)
        return (tmp_path / "dummy.lpm", 0.0, 1, [])

    monkeypatch.setattr(lpm, "run_lpmbuild", fake_run)
    args = lpm.build_parser().parse_args(["rebuild", "base", "--cycle-policy", "group"])
    lpm.cmd_rebuild(args)
    output = capsys.readouterr().out
    assert "[rebuild cycle-group] beta, gamma" in output
    assert calls == ["base", "beta", "gamma", "alpha"]


def test_rebuild_self_cycle_group_with_policy_group(monkeypatch, tmp_path, capsys):
    _prepare_db(
        monkeypatch,
        tmp_path,
        [("solo", json.dumps(["solo"]))],
    )
    p = tmp_path / "packages" / "solo"
    p.mkdir(parents=True)
    (p / "solo.lpmbuild").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(lpm, "run_lpmbuild", lambda *_args, **_kwargs: (tmp_path / "dummy.lpm", 0.0, 1, []))
    args = lpm.build_parser().parse_args(["rebuild", "solo", "--cycle-policy", "group"])
    lpm.cmd_rebuild(args)
    output = capsys.readouterr().out
    assert "[rebuild cycle-group] solo" in output


def test_rebuild_cycle_policy_defaults_to_fail(monkeypatch, tmp_path):
    _prepare_db(
        monkeypatch,
        tmp_path,
        [
            ("base", json.dumps([])),
            ("alpha", json.dumps(["base", "beta"])),
            ("beta", json.dumps(["alpha"])),
        ],
    )
    for pkg in ("base", "alpha", "beta"):
        p = tmp_path / "packages" / pkg
        p.mkdir(parents=True)
        (p / f"{pkg}.lpmbuild").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    args = lpm.build_parser().parse_args(["rebuild", "base"])
    with pytest.raises(SystemExit):
        lpm.cmd_rebuild(args)
