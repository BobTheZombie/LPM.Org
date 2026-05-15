from __future__ import annotations

from src.lpm.app import build_parser
from src.ui.backend import LPMBackend


def test_createiso_parser_wires_command():
    parser = build_parser()
    args = parser.parse_args(["createiso", "--output", "/tmp/system.iso"])
    assert args.cmd == "createiso"
    assert args.source_root == "/"
    assert args.output == "/tmp/system.iso"
    assert args.volume_id == "LPM_PRELOAD"
    assert args.func.__name__ == "cmd_createiso"


def test_backend_create_system_iso_builds_cli_args(monkeypatch):
    backend = LPMBackend()
    captured: dict[str, object] = {}

    def _fake_run_cli(args, *, root=None):
        captured["args"] = list(args)
        captured["root"] = root
        return object()

    monkeypatch.setattr(backend, "run_cli", _fake_run_cli)
    backend.create_system_iso("/tmp/system.iso", source_root="/", volume_id="LPM_PRELOAD")
    assert captured["args"] == [
        "createiso",
        "--source-root",
        "/",
        "--output",
        "/tmp/system.iso",
        "--volume-id",
        "LPM_PRELOAD",
    ]
    assert captured["root"] is None
