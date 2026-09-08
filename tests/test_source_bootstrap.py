import os
from pathlib import Path

from lpm.source_bootstrap import build_and_install_sources, discover_source_packages, source_build_order


def _recipe(path: Path, name: str, requires: str = "") -> None:
    dependency_line = f'REQUIRES=("{requires}")' if requires else "REQUIRES=()"
    path.write_text(f"NAME={name}\nVERSION=1\n{dependency_line}\n", encoding="utf-8")


def test_source_build_order_is_dependency_first(tmp_path: Path) -> None:
    _recipe(tmp_path / "base.lpmbuild", "base")
    _recipe(tmp_path / "shell.lpmbuild", "shell", "base")
    packages = discover_source_packages(tmp_path)
    assert [item.name for item in source_build_order(packages)] == ["base", "shell"]


def test_exact_package_name_wins_over_capability_provider(tmp_path: Path) -> None:
    _recipe(tmp_path / "cargo.lpmbuild", "cargo")
    (tmp_path / "rust.lpmbuild").write_text(
        'NAME=rust\nVERSION=1\nREQUIRES=()\nPROVIDES=("cargo")\n', encoding="utf-8"
    )
    _recipe(tmp_path / "consumer.lpmbuild", "consumer", "cargo")
    order = [item.name for item in source_build_order(discover_source_packages(tmp_path))]
    assert order.index("cargo") < order.index("consumer")


def test_or_dependency_selects_one_local_alternative(tmp_path: Path) -> None:
    _recipe(tmp_path / "a.lpmbuild", "a")
    _recipe(tmp_path / "b.lpmbuild", "b", "consumer")
    _recipe(tmp_path / "consumer.lpmbuild", "consumer", "a | b")
    order = [item.name for item in source_build_order(discover_source_packages(tmp_path))]
    assert order.index("a") < order.index("consumer") < order.index("b")


def test_source_bootstrap_builds_then_installs_in_order(tmp_path: Path) -> None:
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    _recipe(recipes / "base.lpmbuild", "base")
    _recipe(recipes / "shell.lpmbuild", "shell", "base")
    target = tmp_path / "target"
    events: list[str] = []
    state_dirs: list[str | None] = []

    def fake_build(script, **kwargs):
        name = Path(script).stem
        events.append(f"build:{name}")
        state_dirs.append(os.environ.get("LPM_STATE_DIR"))
        artifact = Path(kwargs["outdir"]) / f"{name}.zst"
        artifact.write_bytes(b"pkg")
        return artifact, 0.0, 0, []

    def fake_install(artifact, **kwargs):
        events.append(f"install:{Path(artifact).stem}")
        state_dirs.append(os.environ.get("LPM_STATE_DIR"))

    result = build_and_install_sources(
        recipes, target, tmp_path / "out", build=fake_build, install=fake_install
    )
    assert events == ["build:base", "install:base", "build:shell", "install:shell"]
    assert state_dirs == [str(target / "var/lib/lpm")] * 4
    assert "LPM_STATE_DIR" not in os.environ
    assert result["package_order"] == ["base", "shell"]
    assert (target / "var/lib/lpm/source-bootstrap.json").is_file()


def test_include_closure_selects_virtual_provider(tmp_path: Path) -> None:
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    (recipes / "provider.lpmbuild").write_text(
        'NAME=provider\nVERSION=1\nREQUIRES=()\nPROVIDES=("virtual-api=1")\n',
        encoding="utf-8",
    )
    _recipe(recipes / "consumer.lpmbuild", "consumer", "virtual-api>=1")
    result = build_and_install_sources(
        recipes, tmp_path / "target", tmp_path / "out", dry_run=True,
        include=("consumer",),
    )
    assert result["package_order"] == ["provider", "consumer"]
