import importlib


def test_arch_compatible_accepts_any_and_none_as_universal(monkeypatch):
    lpm = importlib.import_module("lpm.app")
    monkeypatch.setattr(lpm.os, "uname", lambda: type("U", (), {"machine": "x86_64"})())

    assert lpm.arch_compatible("any", "x86_64")
    assert lpm.arch_compatible("none", "x86_64")


def test_arch_compatible_treats_target_any_and_none_as_host_arch(monkeypatch):
    lpm = importlib.import_module("lpm.app")
    monkeypatch.setattr(lpm.os, "uname", lambda: type("U", (), {"machine": "x86_64"})())

    assert lpm.arch_compatible("x86_64", "any")
    assert lpm.arch_compatible("x86_64", "none")
    assert not lpm.arch_compatible("aarch64", "any")
