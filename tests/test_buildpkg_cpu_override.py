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
