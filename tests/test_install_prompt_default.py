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
