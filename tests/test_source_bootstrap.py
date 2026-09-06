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


def test_source_bootstrap_builds_then_installs_in_order(tmp_path: Path) -> None:
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    _recipe(recipes / "base.lpmbuild", "base")
    _recipe(recipes / "shell.lpmbuild", "shell", "base")
    target = tmp_path / "target"
    built: list[str] = []
    installed: list[str] = []

    def fake_build(script, **kwargs):
        name = Path(script).stem
        built.append(name)
        artifact = Path(kwargs["outdir"]) / f"{name}.zst"
        artifact.write_bytes(b"pkg")
        return artifact, 0.0, 0, []

    def fake_install(artifact, **kwargs):
        installed.append(Path(artifact).stem)

    result = build_and_install_sources(
        recipes, target, tmp_path / "out", build=fake_build, install=fake_install
    )
    assert built == ["base", "shell"]
    assert installed == ["base", "shell"]
    assert result["package_order"] == ["base", "shell"]
    assert (target / "var/lib/lpm/source-bootstrap.json").is_file()
