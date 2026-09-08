from argparse import Namespace
from pathlib import Path

from lpm import system_iso
from lpm.app import build_parser


def test_systemiso_parser() -> None:
    args = build_parser().parse_args([
        "systemiso", "--lpmbuild-root", "/recipes", "--package-profile", "/profile",
        "--root", "/target", "--output", "/out.iso", "--dry-run",
    ])
    assert args.architecture == "x86_64-v2"
    assert args.func.__name__ == "cmd_systemiso"


def test_configure_live_root_enables_networking(tmp_path: Path) -> None:
    written = system_iso.configure_live_root(tmp_path, hostname="lpm-test")
    assert written
    assert (tmp_path / "etc/hostname").read_text() == "lpm-test\n"
    nm = tmp_path / "etc/systemd/system/multi-user.target.wants/NetworkManager.service"
    assert nm.is_symlink()
    assert (tmp_path / "etc/resolv.conf").is_symlink()


def test_systemiso_dry_run_resolves_profile(tmp_path: Path, monkeypatch) -> None:
    recipes = tmp_path / "recipes"
    recipes.mkdir()
    (recipes / "base.lpmbuild").write_text("NAME=base\nVERSION=1\nREQUIRES=()\n")
    profile = tmp_path / "profile"
    profile.write_text("base\n")
    args = Namespace(
        lpmbuild_root=str(recipes), package_profile=str(profile), root=str(tmp_path / "root"),
        output=str(tmp_path / "out.iso"), artifact_dir=None, iso_staging=None,
        architecture="x86_64-v2", hostname="lpm-live", volume_id="LPM_LIVE", dry_run=True,
    )
    result = system_iso.build_system_iso(args)
    assert result["package_order"] == ["base"]
    assert result["package_count"] == 1
