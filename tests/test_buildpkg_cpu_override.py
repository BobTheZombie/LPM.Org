import textwrap
from types import SimpleNamespace

import lpm


def test_buildpkg_override_applies_cpu_flags(monkeypatch, tmp_path):
    script = tmp_path / "demo.lpmbuild"
    script.write_text(
        textwrap.dedent(
            """
            NAME=demo
            VERSION=1.0
            RELEASE=1
            ARCH=noarch

            prepare() { :; }
            build() { :; }
            staging() { :; }
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    captured_envs = {}

    def fake_sandboxed_run(func, cwd, env, script_path, stagedir, buildroot, srcroot, aliases=()):
        captured_envs[func] = dict(env)

    monkeypatch.setattr(lpm, "sandboxed_run", fake_sandboxed_run)
    monkeypatch.setattr(lpm, "prompt_install_pkg", lambda *args, **kwargs: None)

    args = SimpleNamespace(
        script=script,
        overrides=["@Override=arch=x86_64v3 -march=x86_64v3 -mtune=generic"],
        outdir=tmp_path,
        no_deps=True,
        install_default=None,
        python_pip=None,
    )

    lpm.cmd_buildpkg(args)

    built = tmp_path / "demo-1.0-1.x86_64v3.zst"
    assert built.exists()

    meta, _ = lpm.read_package_meta(built)
    assert meta.arch == "x86_64v3"

    prepare_env = captured_envs.get("prepare")
    assert prepare_env is not None
    expected_prefix = f"{lpm.OPT_LEVEL} -march=x86_64v3 -mtune=generic"
    assert prepare_env["CFLAGS"].startswith(expected_prefix)
    assert prepare_env["ARCH"] == "x86_64v3"


def test_run_lpmbuild_appends_script_cflags_once(monkeypatch, tmp_path):
    script = tmp_path / "append.lpmbuild"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    extra_flag = "-DWITH_FEATURE"

    script.write_text(
        textwrap.dedent(
            f"""
            NAME=append
            VERSION=1
            RELEASE=1
            ARCH=noarch

            CFLAGS+=" {extra_flag}"

            LOG_DIR="{log_dir.as_posix()}"
            log_flags() {{
                phase="$1"
                printf '%s\n' "$CFLAGS" > "$LOG_DIR/${{phase}}.flags"
            }}

            prepare() {{ log_flags prepare; }}
            build() {{ log_flags build; }}
            staging() {{ log_flags staging; }}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setitem(lpm.CONF, "SANDBOX_MODE", "none")
    monkeypatch.setattr(lpm, "prompt_install_pkg", lambda *args, **kwargs: None)

    out_path, _, _, _ = lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    assert out_path is not None
    for phase in ("prepare", "build", "staging"):
        recorded = (log_dir / f"{phase}.flags").read_text(encoding="utf-8").strip()
        assert recorded.count(extra_flag) == 1
        assert recorded.endswith(extra_flag)


def _capture_envs(monkeypatch):
    captured_envs = {}

    def fake_sandboxed_run(func, cwd, env, script_path, stagedir, buildroot, srcroot, aliases=()):
        captured_envs.setdefault(func, dict(env))

    monkeypatch.setattr(lpm, "sandboxed_run", fake_sandboxed_run)
    return captured_envs


def _write_minimal_script(path, body: str = "") -> None:
    contents = textwrap.dedent(
        f"""
        NAME=demo
        VERSION=1
        RELEASE=1
        ARCH=noarch
        {body}
        prepare() {{ :; }}
        build() {{ :; }}
        staging() {{ :; }}
        """
    ).strip()
    path.write_text(contents + "\n", encoding="utf-8")


def test_run_lpmbuild_disables_auto_optimisation(monkeypatch, tmp_path):
    script = tmp_path / "noopt.lpmbuild"
    _write_minimal_script(script, body="BUILD_OPT=(\"@none!\")")

    captured_envs = _capture_envs(monkeypatch)
    monkeypatch.setattr(lpm, "prompt_install_pkg", lambda *args, **kwargs: None)

    lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    assert captured_envs, "expected sandboxed phases to run"
    for env in captured_envs.values():
        cflags = env.get("CFLAGS", "")
        assert lpm.OPT_LEVEL not in cflags
        assert "-march" not in cflags
        assert "-mtune" not in cflags
        assert lpm.OPT_LEVEL not in env.get("LDFLAGS", "")
        assert env.get("LPM_CPU_MARCH") == "generic"
        assert env.get("LPM_CPU_MTUNE") == "generic"


def test_run_lpmbuild_supports_legacy_buiild_opt(monkeypatch, tmp_path):
    script = tmp_path / "legacy-noopt.lpmbuild"
    _write_minimal_script(script, body="BUIILD_OPT=(\"@none!\")")

    captured_envs = _capture_envs(monkeypatch)
    monkeypatch.setattr(lpm, "prompt_install_pkg", lambda *args, **kwargs: None)

    lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    assert captured_envs, "expected sandboxed phases to run"
    for env in captured_envs.values():
        cflags = env.get("CFLAGS", "")
        assert lpm.OPT_LEVEL not in cflags
        assert "-march" not in cflags
        assert "-mtune" not in cflags


def test_run_lpmbuild_enables_lto(monkeypatch, tmp_path):
    script = tmp_path / "lto.lpmbuild"
    _write_minimal_script(script, body="BUILD_OPT=(\"@lto!=on\")")

    captured_envs = _capture_envs(monkeypatch)
    monkeypatch.setattr(lpm, "prompt_install_pkg", lambda *args, **kwargs: None)

    lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    assert captured_envs, "expected sandboxed phases to run"
    for env in captured_envs.values():
        assert "-flto" in env.get("CFLAGS", "")
        assert "-flto" in env.get("CXXFLAGS", "")
        assert "-flto" in env.get("LDFLAGS", "")


def test_run_lpmbuild_disable_opt_with_lto(monkeypatch, tmp_path):
    script = tmp_path / "noopt-lto.lpmbuild"
    _write_minimal_script(script, body="BUILD_OPT=(\"@none!\" \"@lto!=on\")")

    captured_envs = _capture_envs(monkeypatch)
    monkeypatch.setattr(lpm, "prompt_install_pkg", lambda *args, **kwargs: None)

    lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    assert captured_envs, "expected sandboxed phases to run"
    for env in captured_envs.values():
        cflags = env.get("CFLAGS", "")
        assert lpm.OPT_LEVEL not in cflags
        assert "-march" not in cflags
        assert "-mtune" not in cflags
        assert "-flto" in env.get("CFLAGS", "")
        assert "-flto" in env.get("CXXFLAGS", "")
        assert "-flto" in env.get("LDFLAGS", "")
