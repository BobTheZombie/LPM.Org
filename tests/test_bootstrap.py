from __future__ import annotations

import sys
from pathlib import Path


def _clear_lpm_modules():
    for name in list(sys.modules):
        if name == "lpm" or name.startswith("lpm."):
            sys.modules.pop(name, None)


def test_bootstrap_strips_shadowing_path(tmp_path, monkeypatch):
    shadow_dir = tmp_path / "bin"
    shadow_dir.mkdir()
    (shadow_dir / "lpm.py").write_text("raise RuntimeError('shadowed module imported')\n")

    monkeypatch.syspath_prepend(str(shadow_dir))
    monkeypatch.setattr(sys, "argv", [str(shadow_dir / "lpm")], raising=False)
    _clear_lpm_modules()

    import lpm_bootstrap

    captured: list[list[str] | None] = []

    def fake_loader():
        # The bootstrap should have removed the shadowing directory before we
        # attempt to import the real CLI module.
        assert str(shadow_dir) not in sys.path

        def fake_main(argv=None):
            captured.append(None if argv is None else list(argv))
            return 0

        return fake_main

    monkeypatch.setattr(lpm_bootstrap, "_load_cli_main", fake_loader)

    exit_code = lpm_bootstrap.main(["--version"])

    assert exit_code == 0
    assert captured == [["--version"]]
    assert str(shadow_dir) not in sys.path


def test_bootstrap_imports_packaged_app(tmp_path, monkeypatch):
    install_root = tmp_path / "install"
    bin_dir = install_root / "bin"
    site_packages = install_root / "lib/python3.11/site-packages"

    bin_dir.mkdir(parents=True)
    site_packages.mkdir(parents=True)

    (bin_dir / "lpm.py").write_text("raise RuntimeError('shadowed shim imported')\n")

    package_dir = site_packages / "lpm"
    package_dir.mkdir()

    app_file = package_dir / "app.py"
    app_file.write_text(
        "calls = []\n"
        "def main(argv=None):\n"
        "    calls.append(None if argv is None else list(argv))\n"
        "    return 0\n"
    )
    (package_dir / "__init__.py").write_text("from .app import main\n")

    monkeypatch.setattr(
        sys,
        "path",
        [str(bin_dir), str(site_packages), *[entry for entry in sys.path if entry]],
        raising=False,
    )
    monkeypatch.setattr(sys, "argv", [str(bin_dir / "lpm")], raising=False)

    _clear_lpm_modules()
    sys.modules.pop("lpm_bootstrap", None)

    import importlib

    lpm_bootstrap = importlib.import_module("lpm_bootstrap")

    exit_code = lpm_bootstrap.main(["--fake-flag"])

    assert exit_code == 0
    assert str(bin_dir) not in sys.path

    loaded_app = sys.modules.get("lpm.app")
    assert loaded_app is not None
    assert Path(getattr(loaded_app, "__file__")).resolve() == app_file.resolve()
    assert getattr(loaded_app, "calls") == [["--fake-flag"]]
