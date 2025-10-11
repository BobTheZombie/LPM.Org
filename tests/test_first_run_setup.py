import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lpm
import src.config as config
import src.first_run_ui as first_run_ui


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

    state_dir = tmp_path / "state"
    responses = [
        "native",
        "manual",
        "bwrap",
        "-O3",
        "16",
        "131072",
        str(state_dir),
        "7",
        "400",
        "y",
        "no",
        "x86_64v3",
        "https://example.com/packages/",
        "https://example.com/bin/{name}.lpm",
        "",
        "no",
        "no",
    ]
    user_input = io.StringIO("\n".join(responses) + "\n")
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
    assert "SANDBOX_MODE=bwrap" in text
    assert "OPT_LEVEL=-O3" in text
    assert "FETCH_MAX_WORKERS=16" in text
    assert "IO_BUFFER_SIZE=131072" in text
    assert f"STATE_DIR={state_dir}" in text
    assert "MAX_SNAPSHOTS=7" in text
    assert "MAX_LEARNT_CLAUSES=400" in text
    assert "INSTALL_PROMPT_DEFAULT=y" in text
    assert "ALLOW_LPMBUILD_FALLBACK=false" in text
    assert "CPU_TYPE=x86_64v3" in text
    assert "LPMBUILD_REPO=https://example.com/packages/" in text
    assert "BINARY_REPO=https://example.com/bin/{name}.lpm" in text
    assert "ALWAYS_SIGN=no" in text
    assert "DISTRO_MAINTAINER_MODE=false" in text


def test_gather_metadata_prefers_build_info(monkeypatch, tmp_path):
    info_path = tmp_path / "build-info.json"
    info_path.write_text(
        json.dumps(
            {
                "version": "9.9.9",
                "build_date": "2024-06-01T00:00:00Z",
                "build": "release",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LPM_BUILD_INFO", str(info_path))

    class DummyModule:
        @staticmethod
        def get_runtime_metadata():
            return {
                "name": "LPM",
                "version": "fallback",
                "build": "development",
                "build_date": "",
            }

    monkeypatch.setattr(first_run_ui, "import_module", lambda _: DummyModule)

    metadata = first_run_ui._gather_metadata()

    assert metadata["version"] == "9.9.9"
    assert metadata["build_date"] == "2024-06-01T00:00:00Z"
    assert metadata["build"] == "release"
