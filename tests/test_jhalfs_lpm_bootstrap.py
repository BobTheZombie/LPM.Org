import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "bootstrap" / "jhalfs-lpm"


def test_jhalfs_lpm_shell_sources_parse():
    scripts = [
        PROFILE / "build.sh",
        PROFILE / "scripts" / "validate-root.sh",
        *sorted((PROFILE / "custom").iterdir()),
    ]
    subprocess.run(["bash", "-n", *(str(path) for path in scripts)], check=True)


def test_network_bootstrap_package_order():
    packages = [
        line.strip()
        for line in (PROFILE / "config" / "packages.list").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert packages.index("dbus-broker") < packages.index("NetworkManager")
    assert packages.index("wireless-regdb") < packages.index("wpa_supplicant")
    assert packages.index("wpa_supplicant") < packages.index("NetworkManager")
    assert packages[-1] == "mkinitcpio"


def test_system_profile_selects_broker_and_network_manager():
    profile = (PROFILE / "custom" / "920-lpm-system-config").read_text()
    assert "ln -sfn dbus-broker.service" in profile
    assert "systemctl enable dbus-broker.service" in profile
    assert "systemctl enable NetworkManager.service" in profile
    assert "systemctl enable systemd-resolved.service" in profile
    assert "mkinitcpio -P" in profile


def test_wrapper_help_does_not_require_host_build_dependencies():
    result = subprocess.run(
        [str(PROFILE / "build.sh"), "--help"],
        check=True,
        text=True,
        capture_output=True,
    )
    assert "prepare" in result.stdout
    assert "validate" in result.stdout
