import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lpm
import src.config as config


def test_main_triggers_setup_when_conf_missing(monkeypatch, tmp_path):
    conf_path = tmp_path / "lpm.conf"
    original_conf = dict(config.CONF)
    monkeypatch.setattr(config, "CONF_FILE", conf_path, raising=False)
    monkeypatch.setattr(lpm, "CONF_FILE", conf_path, raising=False)

    captured = {}

    def fake_wizard(*args, **kwargs):
        captured["called"] = True
        config.save_conf({"ARCH": "x86_64"}, path=conf_path)
        return {"ARCH": "x86_64"}

    monkeypatch.setattr(lpm, "run_first_run_wizard", fake_wizard)
    monkeypatch.setattr(lpm, "cmd_repolist", lambda args: None)

    try:
        lpm.main(["repolist"])
    finally:
        config._apply_conf(original_conf)

    assert captured.get("called") is True
    assert conf_path.exists()


def test_setup_command_runs_wizard_and_writes_config(monkeypatch, tmp_path):
    conf_path = tmp_path / "lpm.conf"
    original_conf = dict(config.CONF)
    monkeypatch.setattr(config, "CONF_FILE", conf_path, raising=False)
    monkeypatch.setattr(lpm, "CONF_FILE", conf_path, raising=False)

    user_input = io.StringIO("native\nmanual\ny\nno\nx86_64v3\n")
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdin", user_input)
    monkeypatch.setattr(sys, "stdout", output)

    try:
        lpm.main(["setup"])
    finally:
        config._apply_conf(original_conf)

    text = conf_path.read_text(encoding="utf-8")
    assert "ARCH=native" in text
    assert "INIT_POLICY=manual" in text
    assert "INSTALL_PROMPT_DEFAULT=y" in text
    assert "ALLOW_LPMBUILD_FALLBACK=false" in text
    assert "CPU_TYPE=x86_64v3" in text
