from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


def _reload_atomic_io():
    atomic_io = importlib.import_module("src.atomic_io")
    return importlib.reload(atomic_io)


def _reload_config():
    config = importlib.import_module("src.config")
    return importlib.reload(config)


@pytest.fixture
def state_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    cfg = _reload_config()
    yield cfg


def test_atomic_write_text_respects_umask(state_env, monkeypatch, tmp_path):
    cfg = state_env
    monkeypatch.setattr(cfg, "UMASK", 0o077)
    atomic_io = _reload_atomic_io()

    target = tmp_path / "data" / "value.txt"
    previous_umask = os.umask(0o002)
    try:
        atomic_io.atomic_write_text(target, "hello world\n")
    finally:
        os.umask(previous_umask)

    assert target.read_text(encoding="utf-8") == "hello world\n"
    mode = target.stat().st_mode & 0o777
    assert mode == 0o666 & ~cfg.UMASK

    leftovers = list(target.parent.glob(".*.tmp"))
    assert leftovers == []


def test_atomic_write_json_formats_output(state_env, monkeypatch, tmp_path):
    cfg = state_env
    monkeypatch.setattr(cfg, "UMASK", 0o022)
    atomic_io = _reload_atomic_io()

    target = tmp_path / "data.json"
    atomic_io.atomic_write_json(target, {"b": 2, "a": 1})

    content = target.read_text(encoding="utf-8")
    assert content == '{\n  "a": 1,\n  "b": 2\n}'
    mode = target.stat().st_mode & 0o777
    assert mode == 0o666 & ~cfg.UMASK
