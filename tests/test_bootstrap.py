from __future__ import annotations

import sys


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
