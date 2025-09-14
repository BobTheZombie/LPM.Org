import builtins
import io

import src.config as config


def _mock_cpuinfo(monkeypatch, text):
    original_open = builtins.open

    def mock_open(path, *args, **kwargs):
        if path == "/proc/cpuinfo":
            return io.StringIO(text)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", mock_open)


def test_detect_arm(monkeypatch):
    _mock_cpuinfo(
        monkeypatch,
        "CPU implementer\t: 0x41\nCPU architecture\t: 8\n",
    )
    march, mtune, vendor, family = config._detect_cpu()
    assert march == "armv8-a"
    assert mtune == "armv8-a"
    assert vendor == "0x41"
    assert family == "8"


def test_detect_riscv(monkeypatch):
    _mock_cpuinfo(
        monkeypatch,
        "uarch\t: sifive,u74-mc\nisa\t: rv64imafdc\n",
    )
    march, mtune, vendor, family = config._detect_cpu()
    assert march == "rv64gc"
    assert mtune == "rv64gc"
    assert vendor == "sifive,u74-mc"
    assert family == "rv64imafdc"
