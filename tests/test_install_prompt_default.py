"""Regression tests for install confirmation and default Yes behavior."""
from types import SimpleNamespace
import pytest
from lpm import app as lpm


@pytest.fixture
def prompt(monkeypatch, tmp_path):
    blob = tmp_path / "demo.lpm"
    installed = []
    def read_meta(path):
        return SimpleNamespace(name="demo", version="1", release="1", arch="x86_64"), []
    def install(path, *, explicit):
        installed.append((path, explicit))
    monkeypatch.setattr(lpm, "_resolve_lpm_attr", lambda key, fallback: {
        "read_package_meta": read_meta, "installpkg": install,
    }.get(key, fallback))
    return blob, installed


@pytest.mark.parametrize("default,response,expected", [
    ("y", "", True), ("n", "", False), ("y", "n", False),
    ("n", "y", True), ("y", "YES", True), ("y", "no", False),
])
def test_answer_and_default(monkeypatch, prompt, default, response, expected):
    blob, installed = prompt
    questions = []
    def input_reply(question):
        questions.append(question)
        return response
    monkeypatch.setattr("builtins.input", input_reply)
    assert lpm.prompt_install_pkg(blob, default=default) is expected
    assert installed == ([(blob, True)] if expected else [])
    assert ("[Y/n]" if default == "y" else "[y/N]") in questions[0]


def test_live_config_not_stale_import(monkeypatch, prompt):
    blob, installed = prompt
    monkeypatch.setattr(lpm._config, "INSTALL_PROMPT_DEFAULT", "y")
    monkeypatch.setattr(lpm, "INSTALL_PROMPT_DEFAULT", "n")
    monkeypatch.setattr("builtins.input", lambda question: "")
    assert lpm.prompt_install_pkg(blob) is True
    assert installed == [(blob, True)]


def test_explicit_no_overrides_config_yes(monkeypatch, prompt):
    blob, installed = prompt
    monkeypatch.setattr(lpm._config, "INSTALL_PROMPT_DEFAULT", "y")
    monkeypatch.setattr("builtins.input", lambda question: "")
    assert lpm.prompt_install_pkg(blob, default="n") is False
    assert installed == []


def test_invalid_answer_reprompts(monkeypatch, prompt):
    blob, installed = prompt
    answers = iter(["maybe", ""])
    questions = []
    def ask(question):
        questions.append(question)
        return next(answers)
    monkeypatch.setattr("builtins.input", ask)
    assert lpm.prompt_install_pkg(blob, default="y") is True
    assert len(questions) == 2
    assert installed == [(blob, True)]


def test_dependency_not_marked_explicit(monkeypatch, prompt):
    blob, installed = prompt
    monkeypatch.setattr("builtins.input", lambda question: "")
    assert lpm.prompt_install_pkg(blob, kind="dependency", default="y") is True
    assert installed == [(blob, False)]


def test_eof_never_installs(monkeypatch, prompt, capsys):
    blob, installed = prompt
    def eof(question):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    assert lpm.prompt_install_pkg(blob, default="y") is False
    assert installed == []
    assert "EOF" in capsys.readouterr().err


def test_installer_error_propagates(monkeypatch, prompt):
    blob, _ = prompt
    original = lpm._resolve_lpm_attr
    def fail(path, *, explicit):
        raise RuntimeError("disk full")
    monkeypatch.setattr(lpm, "_resolve_lpm_attr",
                        lambda key, fallback: fail if key == "installpkg" else original(key, fallback))
    monkeypatch.setattr("builtins.input", lambda question: "")
    with pytest.raises(RuntimeError, match="disk full"):
        lpm.prompt_install_pkg(blob, default="y")


@pytest.mark.parametrize("default", ["y", "n", None])
def test_auto_yes_installs_without_reading_stdin(monkeypatch, prompt, default):
    blob, installed = prompt
    monkeypatch.setattr(lpm._config, "INSTALL_PROMPT_DEFAULT", "n")
    monkeypatch.setattr("builtins.input", lambda question: pytest.fail("stdin read in --yes mode"))
    assert lpm.prompt_install_pkg(blob, default=default, assume_yes=True) is True
    assert installed == [(blob, True)]


def test_auto_yes_dependency_is_not_explicit(monkeypatch, prompt):
    blob, installed = prompt
    monkeypatch.setattr("builtins.input", lambda question: pytest.fail("stdin read in --yes mode"))
    assert lpm.prompt_install_pkg(blob, kind="dependency", assume_yes=True) is True
    assert installed == [(blob, False)]


def test_auto_yes_does_not_ignore_install_errors(monkeypatch, prompt):
    blob, _ = prompt
    original = lpm._resolve_lpm_attr

    def fail(path, *, explicit):
        raise RuntimeError("protected package or invalid signature")

    monkeypatch.setattr(
        lpm, "_resolve_lpm_attr",
        lambda name, fallback: fail if name == "installpkg" else original(name, fallback),
    )
    monkeypatch.setattr("builtins.input", lambda question: pytest.fail("unexpected input"))
    with pytest.raises(RuntimeError, match="protected package"):
        lpm.prompt_install_pkg(blob, assume_yes=True)


@pytest.mark.parametrize("command,args", [
    ("buildpkg", ["demo"]),
    ("build", ["/tmp/staged", "--name", "demo", "--version", "1"]),
    ("rebuild", ["demo"]),
    ("rebuild-stage2", []),
    ("install", ["demo"]),
    ("installpkg", ["demo.lpm"]),
    ("upgrade", []),
    ("upgradepkg", []),
])
@pytest.mark.parametrize("flag", ["-y", "--yes", "--noconfirm"])
def test_unattended_flag_accepted(command, args, flag):
    options = lpm.build_parser().parse_args([command, *args, flag])
    assert options.assume_yes is True


def test_install_default_yes_still_reads_input(monkeypatch, prompt):
    blob, installed = prompt
    calls = []
    def respond(question):
        calls.append(question)
        return ""
    monkeypatch.setattr("builtins.input", respond)
    assert lpm.prompt_install_pkg(blob, default="y") is True
    assert len(calls) == 1
    assert installed == [(blob, True)]


def test_reject_yes_and_explicit_no(monkeypatch, capsys):
    from types import SimpleNamespace
    with pytest.raises(SystemExit) as excinfo:
        lpm._reject_conflicting_install_flags(
            SimpleNamespace(assume_yes=True, install_default="n")
        )
    assert excinfo.value.code == 2
    assert "--yes/--noconfirm conflicts" in capsys.readouterr().err


def test_buildpkg_cli_propagates_yes(monkeypatch, tmp_path):
    from types import SimpleNamespace
    recipe = tmp_path / "demo.lpmbuild"
    recipe.write_text('NAME="demo"\n')
    artifact = tmp_path / "demo.lpm"
    artifact.write_bytes(b"test")
    recorded = []

    def fake_run(script, outdir, **kwargs):
        recorded.append(kwargs)
        return artifact, 0.1, 1, []

    def resolve(name, fallback):
        if name == "run_lpmbuild":
            return fake_run
        if name == "read_package_meta":
            return lambda _: (SimpleNamespace(name="demo", version="1", release="1",
                                               arch="any", requires=[]), [])
        return fallback

    monkeypatch.setattr(lpm, "_resolve_lpm_attr", resolve)
    monkeypatch.setattr(lpm, "resolve_buildpkg_recipe", lambda name, fetch: (recipe, "local"))
    monkeypatch.setattr(lpm, "print_build_summary", lambda *args: None)
    args = lpm.build_parser().parse_args(
        ["buildpkg", str(recipe), "--yes", "--no-deps", "--outdir", str(tmp_path)]
    )
    lpm.cmd_buildpkg(args)
    assert len(recorded) == 1
    assert recorded[0]["assume_yes"] is True
    assert recorded[0]["build_deps"] is False


def test_stage2_rebuild_passes_yes_to_runner(monkeypatch, tmp_path):
    from lpm import stage2
    recorded = []

    def fake_run(script, outdir, **kwargs):
        recorded.append(kwargs)
        return None

    def fake_drain(state, installed, build, fetch, **kwargs):
        build(tmp_path / "demo.lpmbuild", tmp_path, 2, {})
        return {}

    monkeypatch.setattr(lpm, "run_lpmbuild", fake_run)
    monkeypatch.setattr(stage2, "drain", fake_drain)
    lpm._run_stage2_queue(assume_yes=True)
    assert len(recorded) == 1
    assert recorded[0]["assume_yes"] is True
    assert recorded[0]["_build_stage"] == 2
