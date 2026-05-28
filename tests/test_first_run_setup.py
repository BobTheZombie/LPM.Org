import contextlib
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lpm
import src.config as config
import src.first_run_ui as first_run_ui


@contextlib.contextmanager
def _recording_operation_phase(events, *, active_uid=None):
    events.append(("enter", True, active_uid["current"] if active_uid else None))
    previous_uid = None
    if active_uid is not None:
        previous_uid = active_uid["current"]
        active_uid["current"] = active_uid["privileged"]
    try:
        yield
    finally:
        if active_uid is not None:
            active_uid["current"] = previous_uid
        events.append(("exit", True, active_uid["current"] if active_uid else None))


def test_setup_command_enters_privileged_section_before_wizard(monkeypatch):
    import importlib

    app = importlib.import_module("lpm.app")
    events = []

    def fake_operation_phase(*, privileged=True):
        assert privileged is True
        return _recording_operation_phase(events)

    def fake_wizard(*args, **kwargs):
        events.append(("wizard", None, None))
        return {"ARCH": "x86_64"}

    monkeypatch.setattr(app, "operation_phase", fake_operation_phase)
    monkeypatch.setattr(app, "run_first_run_wizard", fake_wizard)

    lpm.main(["setup"])

    assert events == [
        ("enter", True, None),
        ("wizard", None, None),
        ("exit", True, None),
    ]


def test_auto_setup_enters_privileged_section_before_wizard(monkeypatch, tmp_path):
    import importlib

    app = importlib.import_module("lpm.app")
    conf_path = tmp_path / "missing.conf"
    events = []

    def fake_operation_phase(*, privileged=True):
        assert privileged is True
        return _recording_operation_phase(events)

    def fake_wizard(*args, **kwargs):
        events.append(("wizard", None, None))
        conf_path.write_text("ARCH=x86_64\n", encoding="utf-8")
        return {"ARCH": "x86_64"}

    def fake_repolist(args):
        events.append(("command", None, None))

    monkeypatch.setattr(app, "CONF_FILE", conf_path, raising=False)
    monkeypatch.setattr(lpm, "CONF_FILE", conf_path, raising=False)
    monkeypatch.setattr(app, "operation_phase", fake_operation_phase)
    monkeypatch.setattr(app, "run_first_run_wizard", fake_wizard)
    monkeypatch.setattr(app, "cmd_repolist", fake_repolist)

    lpm.main(["repolist"])

    assert events == [
        ("enter", True, None),
        ("wizard", None, None),
        ("exit", True, None),
        ("command", None, None),
    ]


def test_auto_setup_re_drops_sudo_style_privileges_before_command(monkeypatch, tmp_path):
    import importlib

    app = importlib.import_module("lpm.app")
    conf_path = tmp_path / "missing.conf"
    # Simulate the privilege manager's sudo path: the process started as root,
    # then module initialization dropped the effective UID before command work.
    active_uid = {"started": 0, "privileged": 0, "current": 1000}
    seen = []

    def fake_operation_phase(*, privileged=True):
        assert privileged is True
        return _recording_operation_phase(seen, active_uid=active_uid)

    def fake_wizard(*args, **kwargs):
        seen.append(("wizard_euid", active_uid["current"]))
        conf_path.write_text("ARCH=x86_64\n", encoding="utf-8")
        return {"ARCH": "x86_64"}

    def fake_repolist(args):
        seen.append(("command_euid", active_uid["current"]))

    monkeypatch.setattr(app, "CONF_FILE", conf_path, raising=False)
    monkeypatch.setattr(lpm, "CONF_FILE", conf_path, raising=False)
    monkeypatch.setattr(app, "operation_phase", fake_operation_phase)
    monkeypatch.setattr(app, "run_first_run_wizard", fake_wizard)
    monkeypatch.setattr(app, "cmd_repolist", fake_repolist)

    lpm.main(["repolist"])

    assert active_uid["started"] == 0
    assert ("wizard_euid", 0) in seen
    assert ("command_euid", 1000) in seen
    assert active_uid["current"] == 1000


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


def test_wizard_permission_denied_reports_actionable_message(monkeypatch, tmp_path):
    conf_path = tmp_path / "etc" / "lpm" / "lpm.conf"
    output = io.StringIO()

    monkeypatch.setattr(first_run_ui, "_build_fields", lambda: ([], []))

    def deny_save(settings, *, path):
        raise PermissionError("blocked")

    monkeypatch.setattr(first_run_ui.config, "save_conf", deny_save)

    with pytest.raises(first_run_ui.FirstRunSetupError) as excinfo:
        first_run_ui.run_first_run_wizard(
            conf_path=conf_path,
            input_stream=io.StringIO(),
            output_stream=output,
            metadata={"version": "test", "build": "test", "build_date": ""},
            init_system="unknown",
            cpu_info={
                "vendor": "",
                "family": "",
                "march": "generic",
                "mtune": "generic",
            },
        )

    text = output.getvalue()
    assert str(conf_path) in text
    assert "sudo lpm setup" in text
    assert str(conf_path) in str(excinfo.value)
    assert "sudo lpm setup" in str(excinfo.value)


def test_main_reports_first_run_setup_errors_without_traceback(monkeypatch, tmp_path, capsys):
    import importlib

    app = importlib.import_module("lpm.app")

    conf_path = tmp_path / "missing.conf"
    message = first_run_ui.permission_denied_message(conf_path)

    def fake_wizard(*args, **kwargs):
        raise app.FirstRunSetupError(message)

    monkeypatch.setattr(lpm, "CONF_FILE", conf_path, raising=False)
    monkeypatch.setattr(lpm, "run_first_run_wizard", fake_wizard)
    monkeypatch.setattr(lpm, "cmd_repolist", lambda args: None)

    with pytest.raises(SystemExit) as excinfo:
        lpm.main(["repolist"])

    captured = capsys.readouterr()
    assert excinfo.value.code == 2
    assert str(conf_path) in captured.err
    assert "sudo lpm setup" in captured.err
    assert "Traceback" not in captured.err


def test_setup_command_runs_wizard_and_writes_config(monkeypatch, tmp_path):
    conf_path = tmp_path / "lpm.conf"
    original_conf = dict(config.CONF)
    monkeypatch.setattr(config, "CONF_FILE", conf_path, raising=False)
    monkeypatch.setattr(lpm, "CONF_FILE", conf_path, raising=False)

    state_dir = tmp_path / "state"
    monkeypatch.setattr(config, "MARCH", "znver2", raising=False)
    monkeypatch.setattr(config, "MTUNE", "znver2", raising=False)
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
        "0.9",
        "0.998",
        "6",
        "y",
        "no",
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
    expected_prompt = (
        "Enable automatic optimisation using -march=znver2 / -mtune=znver2? (yes/no)"
    )
    assert expected_prompt in output.getvalue()
    assert "ARCH=native" in text
    assert "INIT_POLICY=manual" in text
    assert "SANDBOX_MODE=bwrap" in text
    assert "OPT_LEVEL=-O3" in text
    assert "FETCH_MAX_WORKERS=16" in text
    assert "IO_BUFFER_SIZE=131072" in text
    assert f"STATE_DIR={state_dir}" in text
    assert "MAX_SNAPSHOTS=7" in text
    assert "MAX_LEARNT_CLAUSES=400" in text
    assert "VSIDS_VAR_DECAY=0.9" in text
    assert "VSIDS_CLAUSE_DECAY=0.998" in text
    assert "BUILDPKG_WORKERS=6" in text
    assert "INSTALL_PROMPT_DEFAULT=y" in text
    assert "ALLOW_LPMBUILD_FALLBACK=false" in text
    assert "ENABLE_CPU_OPTIMIZATIONS=false" in text
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
