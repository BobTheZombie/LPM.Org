import textwrap

import pytest

import lpm


def _write_recipe(path, options):
    rendered = "\n".join(f'    "{option}"' for option in options)
    path.write_text(
        textwrap.dedent(
            f"""
            NAME=build-options-test
            VERSION=1
            RELEASE=1
            ARCH=x86_64

            BUILD_OPTIONS=(
            {rendered}
            )

            prepare() {{ :; }}
            build() {{ :; }}
            staging() {{ :; }}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )


def test_metadata_captures_build_options(tmp_path):
    recipe = tmp_path / "options.lpmbuild"
    _write_recipe(recipe, ["@--stripping", "@--lto", "@--optimize=3"])

    _, arrays, _ = lpm._capture_lpmbuild_metadata(recipe)

    assert arrays["BUILD_OPTIONS"] == [
        "@--stripping",
        "@--lto",
        "@--optimize=3",
    ]


def test_metadata_reports_bash_syntax_error(tmp_path):
    recipe = tmp_path / "broken.lpmbuild"
    recipe.write_text('NAME="broken"\nif then\n', encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        lpm._capture_lpmbuild_metadata(recipe)

    message = str(excinfo.value)
    assert str(recipe) in message
    assert "syntax error" in message


def test_recipe_optimization_overrides_config_and_host_flags(monkeypatch, tmp_path):
    recipe = tmp_path / "override.lpmbuild"
    _write_recipe(recipe, ["@--lto", "@--optimize=3"])
    captured = {}

    def fake_sandboxed_run(func, cwd, env, script_path, stagedir, buildroot, srcroot, aliases=()):
        captured[func] = dict(env)

    monkeypatch.setenv("CFLAGS", "-O0 -DHOST_FLAG")
    monkeypatch.setenv("CXXFLAGS", "-Os -DHOST_CXX_FLAG")
    monkeypatch.setenv("LDFLAGS", "-Og -Wl,--as-needed")
    monkeypatch.setattr(lpm, "sandboxed_run", fake_sandboxed_run)

    lpm.run_lpmbuild(
        recipe,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    assert captured
    for env in captured.values():
        for variable in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
            flags = env[variable].split()
            assert "-O3" in flags
            assert "-flto" in flags
            assert not {"-O0", "-O1", "-O2", "-Os", "-Og", "-Ofast", "-Oz"}.intersection(flags)
        assert env["LPM_BUILD_OPTIMIZE"] == "3"
        assert env["LPM_BUILD_LTO"] == "1"


@pytest.mark.parametrize(
    "options",
    [
        ["@--optimize=fast"],
        ["@--optimize=2", "@--optimize=3"],
        ["@--unknown"],
    ],
)
def test_invalid_build_options_are_rejected(tmp_path, options):
    recipe = tmp_path / "invalid.lpmbuild"
    _write_recipe(recipe, options)

    with pytest.raises(SystemExit):
        lpm.run_lpmbuild(
            recipe,
            outdir=tmp_path,
            prompt_install=False,
            build_deps=False,
        )
