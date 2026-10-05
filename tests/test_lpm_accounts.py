from pathlib import Path
from types import SimpleNamespace

from lpm.bootstrap import BootstrapConfig, load_config


ROOT = Path(__file__).resolve().parents[1]


def test_lpm_sysusers_defines_separate_locked_build_account() -> None:
    config = (ROOT / "usr/lib/sysusers.d/lpm.conf").read_text(encoding="utf-8")
    assert "g lpm-build" in config
    assert (
        'u lpm-build       -  "LPM package build account" '
        "/var/lib/lpm-build      /usr/bin/nologin"
    ) in config
    assert "m lpm-build lpm" not in config


def test_lpm_tmpfiles_limits_build_account_to_build_tree() -> None:
    config = (ROOT / "usr/lib/tmpfiles.d/lpm.conf").read_text(encoding="utf-8")
    assert "d /var/lib/lpm-build 0750 lpm-build lpm-build" in config
    assert "d /var/lib/lpm/cache 0770 root lpm" in config
    assert "d /var/lib/lpm/snapshots 0770 root lpm" in config


def test_bootstrap_defaults_to_lpm_build_account(tmp_path: Path) -> None:
    direct = BootstrapConfig(target=tmp_path / "root")
    loaded = load_config(
        SimpleNamespace(target=str(tmp_path / "other"), config=None)
    )
    assert direct.lfs_user == "lpm-build"
    assert loaded.lfs_user == "lpm-build"
