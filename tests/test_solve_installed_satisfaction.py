import pytest

from src.lpm.app import PkgMeta, ResolutionError, Universe, register_universe_candidate, solve


def _universe(installed=None):
    return Universe(
        candidates_by_name={},
        providers={},
        installed=installed or {},
        pins={},
        holds=set(),
    )


def test_meta_package_dependency_satisfied_by_installed_when_repo_missing_dep():
    universe = _universe(installed={"dep": {"version": "1.0", "provides": []}})
    register_universe_candidate(
        universe,
        PkgMeta(name="meta", version="1.0", requires=["dep"]),
    )

    solved = solve(["meta"], universe)

    assert [pkg.name for pkg in solved] == ["meta"]


def test_goal_satisfied_by_installed_provides_when_repo_empty():
    universe = _universe(
        installed={"provider": {"version": "2.0", "provides": ["virtual-dep"]}}
    )

    solved = solve(["virtual-dep"], universe)

    assert solved == []


def test_version_constrained_goal_satisfied_by_installed_version():
    universe = _universe(installed={"libfoo": {"version": "3.2", "provides": []}})

    solved = solve(["libfoo>=3.0"], universe)

    assert solved == []


def test_no_provider_error_when_not_installed_and_no_repo_candidate():
    universe = _universe(installed={"other": {"version": "1.0", "provides": []}})

    with pytest.raises(ResolutionError) as excinfo:
        solve(["missing>=2.0"], universe)

    assert "No provider for goal 'missing>=2.0'" in str(excinfo.value)
