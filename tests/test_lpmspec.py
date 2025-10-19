from __future__ import annotations

import argparse
import json

from src.lpm import app


def _get_config():
    """Return the configuration module used by :mod:`src.lpm.app`."""

    return app._config


def _get_maintainer_config():
    """Return the configuration module referenced by maintainer mode."""

    return app.maintainer_mode.config


def test_generate_lpmspec_creates_spec(tmp_path):
    config = _get_config()
    maint_config = _get_maintainer_config()
    snapshot = {
        "DISTRO_MAINTAINER_MODE": config.DISTRO_MAINTAINER_MODE,
        "DISTRO_LPMSPEC_PATH": config.DISTRO_LPMSPEC_PATH,
        "_MAINTAINER_DISTRO_MAINTAINER_MODE": maint_config.DISTRO_MAINTAINER_MODE,
    }
    try:
        config.DISTRO_MAINTAINER_MODE = True
        maint_config.DISTRO_MAINTAINER_MODE = True

        # default location
        app.cmd_generate_lpmspec(argparse.Namespace(output=None))
        spec_path = config.DISTRO_LPMSPEC_PATH
        data = json.loads(spec_path.read_text(encoding="utf-8"))

        assert data["api_version"] == app.LPMSPEC_API_VERSION
        assert data["lpm"]["name"]
        commands = {cmd["name"] for cmd in data["cli"]["commands"]}
        assert "install" in commands
        assert "lpmspec" in commands

        # custom output path
        custom_path = tmp_path / "custom.json"
        app.cmd_generate_lpmspec(argparse.Namespace(output=custom_path))
        custom = json.loads(custom_path.read_text(encoding="utf-8"))
        assert custom["cli"]["commands"] == data["cli"]["commands"]
    finally:
        for key, value in snapshot.items():
            if key == "_MAINTAINER_DISTRO_MAINTAINER_MODE":
                maint_config.DISTRO_MAINTAINER_MODE = value
            else:
                setattr(config, key, value)
