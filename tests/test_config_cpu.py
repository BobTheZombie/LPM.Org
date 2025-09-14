import builtins
import io
import os
import sys
import logging
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import src.config as config


def mock_cpuinfo(monkeypatch, data: str) -> None:
    original_open = builtins.open

    def fake_open(path, *args, **kwargs):
        if path == "/proc/cpuinfo":
            return io.StringIO(data)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fake_open)


CPUINFO_SNB = """vendor_id\t: GenuineIntel
cpu family\t: 6
model\t\t: 42
flags\t\t: fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat pse36 clflush dts acpi mmx fxsr sse sse2 ss ht tm pbe syscall nx rdtscp lm constant_tsc arch_perfmon pebs bts rep_good nopl xtopology nonstop_tsc aperfmperf pni pclmulqdq dtes64 monitor ds_cpl vmx smx est tm2 ssse3 cx16 xtpr pdcm pcid dca sse4_1 sse4_2 x2apic movbe popcnt tsc_deadline_timer aes xsave avx f16c rdrand lahf_lm ida arat xsaveopt pln pts dtherm
"""

CPUINFO_HASWELL = """vendor_id\t: GenuineIntel
cpu family\t: 6
model\t\t: 60
flags\t\t: fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat pse36 clflush dts acpi mmx fxsr sse sse2 ss ht tm pbe syscall nx rdtscp lm constant_tsc arch_perfmon pebs bts rep_good nopl xtopology nonstop_tsc aperfmperf pni pclmulqdq dtes64 monitor ds_cpl vmx smx est tm2 ssse3 cx16 xtpr pdcm pcid dca sse4_1 sse4_2 x2apic movbe popcnt tsc_deadline_timer aes xsave avx f16c rdrand lahf_lm ida arat xsaveopt pln pts dtherm avx2 bmi1 bmi2 fma
"""

CPUINFO_SKX = """vendor_id\t: GenuineIntel
cpu family\t: 6
model\t\t: 85
flags\t\t: fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat pse36 clflush dts acpi mmx fxsr sse sse2 ss ht tm pbe syscall nx rdtscp lm constant_tsc arch_perfmon pebs bts rep_good nopl xtopology nonstop_tsc aperfmperf pni pclmulqdq dtes64 monitor ds_cpl vmx smx est tm2 ssse3 cx16 xtpr pdcm pcid dca sse4_1 sse4_2 x2apic movbe popcnt tsc_deadline_timer aes xsave avx f16c rdrand lahf_lm ida arat xsaveopt pln pts dtherm avx2 bmi1 bmi2 fma avx512f avx512cd avx512dq avx512bw avx512vl
"""

CPUINFO_GENERIC = """vendor_id\t: GenuineIntel
cpu family\t: 6
model\t\t: 1
flags\t\t: fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat pse36 clflush dts acpi mmx fxsr sse sse2
"""


def test_detect_intel_v2(monkeypatch):
    mock_cpuinfo(monkeypatch, CPUINFO_SNB)
    march, mtune, vendor, family = config._detect_cpu()
    assert march == mtune == "x86-64-v2"
    assert vendor == "GenuineIntel"
    assert family == "6"


def test_detect_intel_v3(monkeypatch):
    mock_cpuinfo(monkeypatch, CPUINFO_HASWELL)
    march, mtune, vendor, family = config._detect_cpu()
    assert march == mtune == "x86-64-v3"


def test_detect_intel_v4(monkeypatch):
    mock_cpuinfo(monkeypatch, CPUINFO_SKX)
    march, mtune, vendor, family = config._detect_cpu()
    assert march == mtune == "x86-64-v4"


def test_detect_intel_generic(monkeypatch):
    mock_cpuinfo(monkeypatch, CPUINFO_GENERIC)
    march, mtune, vendor, family = config._detect_cpu()
    assert march == mtune == "generic"


@pytest.mark.parametrize("decl, expected", [
    ("x86_64v2", "x86-64-v2"),
    ("x86-64-v3", "x86-64-v3"),
])
def test_cpu_type_override(monkeypatch, decl, expected):
    def fail_detect() -> tuple[str, str, str, str]:  # pragma: no cover - should not run
        raise AssertionError("_detect_cpu should not be called")

    monkeypatch.setattr(config, "_detect_cpu", fail_detect)
    config.CONF["CPU_TYPE"] = decl
    config.MARCH = config.MTUNE = config.CPU_VENDOR = config.CPU_FAMILY = ""
    config.MARCH, config.MTUNE, config.CPU_VENDOR, config.CPU_FAMILY = config._init_cpu_settings()
    assert config.MARCH == config.MTUNE == expected
    assert config.CPU_VENDOR == config.CPU_FAMILY == ""
    config.CONF.pop("CPU_TYPE", None)


@pytest.mark.parametrize("decl", ["x86-64-v5", "gibberish"])
def test_cpu_type_invalid_falls_back(monkeypatch, caplog, decl):
    expected = ("x86-64-v2", "x86-64-v2", "Vendor", "6")

    def fake_detect() -> tuple[str, str, str, str]:
        return expected

    monkeypatch.setattr(config, "_detect_cpu", fake_detect)
    config.CONF["CPU_TYPE"] = decl
    config.MARCH = config.MTUNE = config.CPU_VENDOR = config.CPU_FAMILY = ""

    with caplog.at_level(logging.WARNING):
        config.MARCH, config.MTUNE, config.CPU_VENDOR, config.CPU_FAMILY = config._init_cpu_settings()

    assert (config.MARCH, config.MTUNE, config.CPU_VENDOR, config.CPU_FAMILY) == expected
    assert "Unrecognized CPU_TYPE" in caplog.text
    config.CONF.pop("CPU_TYPE", None)
