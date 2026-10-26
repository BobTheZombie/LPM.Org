"""Source recipe planning shares the transactional SAT encoder."""
from pathlib import Path

import pytest
from src.lpm.app import _capture_lpmbuild_metadata
from src.lpm.buildpkg_sat import plan_source_build


def recipe(path, name, requires=(), build_requires=(), stage2=(), provides=()):
    def arr(value):
        return "(" + " ".join(repr(x) for x in value) + ")"
    path.write_text(
        f"NAME={name!r}\nVERSION='6.12.0'\nARCH='noarch'\n"
        f"REQUIRES={arr(requires)}\n"
        f"BUILD_REQUIRES={arr(build_requires)}\n"
        f"STAGE_2_REQUIRES={arr(stage2)}\n"
        f"PROVIDES={arr(provides)}\n"
        "prepare() { :; }\nbuild() { :; }\nstaging() { :; }\n"
    )
    return path


def build_plan(tmp_path, main, recipes, installed=None):
    count = []
    def fetch(name, destination):
        count.append(name)
        if name not in recipes:
            raise OSError("no remote package named " + name)
        destination.write_text(recipes[name].read_text())
        return destination
    scal, arr, _ = _capture_lpmbuild_metadata(main)
    result = plan_source_build(
        main, scal, arr, installed or {}, fetch, _capture_lpmbuild_metadata
    )
    return result, count


def test_versioned_dependency_uses_new_source_not_installed_old(tmp_path):
    main = recipe(tmp_path / "root.lpmbuild", "root", build_requires=["qt6-base>=6.12.0"])
    qt = recipe(tmp_path / "qt.lpmbuild", "qt6-base")
    plan, fetched = build_plan(
        tmp_path, main, {"qt6-base": qt},
        {"qt6-base": {"version": "6.11.2", "provides": []}},
    )
    assert fetched == ["qt6-base"]
    assert plan.direct_for("root") == ["qt6-base"]


def test_installed_compatible_version_needs_no_source_build(tmp_path):
    main = recipe(tmp_path / "root.lpmbuild", "root", ["qt6-base>=6.12.0"])
    plan, fetched = build_plan(
        tmp_path, main, {},
        {"qt6-base": {"version": "6.12.0", "provides": []}},
    )
    assert not fetched
    assert plan.direct_for("root") == []


def test_diamond_dependencies_build_shared_prerequisite_once(tmp_path):
    main = recipe(tmp_path / "root.lpmbuild", "root", ["a", "b"])
    a = recipe(tmp_path / "a.lpmbuild", "a", ["c"])
    b = recipe(tmp_path / "b.lpmbuild", "b", ["c"])
    c = recipe(tmp_path / "c.lpmbuild", "c")
    plan, _ = build_plan(tmp_path, main, {"a": a, "b": b, "c": c})
    assert set(plan.direct_for("root")) == {"a", "b"}
    assert plan.direct_for("a") == ["c"]
    assert plan.direct_for("b") == ["c"]


def test_sat_selects_available_or_alternative(tmp_path):
    main = recipe(tmp_path / "root.lpmbuild", "root", ["missing | working"])
    working = recipe(tmp_path / "working.lpmbuild", "working")
    plan, _ = build_plan(tmp_path, main, {"working": working})
    assert plan.direct_for("root") == ["working"]


def test_deferred_stage2_edge_not_eager_build_dependency(tmp_path):
    main = recipe(tmp_path / "root.lpmbuild", "root", ["bootstrap"])
    child = recipe(tmp_path / "bootstrap.lpmbuild", "bootstrap", stage2=["root"])
    plan, _ = build_plan(tmp_path, main, {"bootstrap": child})
    assert plan.direct_for("root") == ["bootstrap"]
    assert plan.direct_for("bootstrap") == []
