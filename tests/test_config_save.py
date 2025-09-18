import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import src.config as config


def test_save_conf_round_trip_updates_globals(tmp_path):
    target = tmp_path / "lpm.conf"
    original_conf = dict(config.CONF)
    try:
        config.save_conf(
            {
                "opt_level": "-O3",
                "allow_lpmbuild_fallback": True,
                "copy_out_dir": "/tmp/out",
                "cpu_type": "x86_64v3",
                "alpha": 1,
                "zeta": 2,
                "bad key": "ignored",
            },
            path=target,
        )

        text = target.read_text(encoding="utf-8")
        assert "# LPM configuration file" in text
        assert "OPT_LEVEL=-O3" in text
        assert "ALLOW_LPMBUILD_FALLBACK=true" in text
        assert "COPY_OUT_DIR=/tmp/out" in text
        assert "CPU_TYPE=x86_64v3" in text
        assert "bad key" not in text

        lines = text.splitlines()
        alpha_idx = next(i for i, ln in enumerate(lines) if ln.startswith("ALPHA="))
        zeta_idx = next(i for i, ln in enumerate(lines) if ln.startswith("ZETA="))
        assert alpha_idx < zeta_idx

        conf = config.load_conf(target)
        assert conf["ALLOW_LPMBUILD_FALLBACK"] == "true"
        assert conf["ALPHA"] == "1"
        assert conf["ZETA"] == "2"

        assert config.CONF["COPY_OUT_DIR"] == "/tmp/out"
        assert config.OPT_LEVEL == "-O3"
        assert config.ALLOW_LPMBUILD_FALLBACK is True
        assert config.MARCH == config.MTUNE == "x86-64-v3"
    finally:
        config._apply_conf(original_conf)


def test_save_conf_normalizes_invalid_values(tmp_path):
    target = tmp_path / "lpm_invalid.conf"
    original_conf = dict(config.CONF)
    try:
        config.save_conf(
            {
                "OPT_LEVEL": "bogus",
                "MAX_LEARNT_CLAUSES": "oops",
                "MAX_SNAPSHOTS": "-5",
                "INSTALL_PROMPT_DEFAULT": "maybe",
                "ALLOW_LPMBUILD_FALLBACK": False,
            },
            path=target,
        )

        text = target.read_text(encoding="utf-8")
        assert "OPT_LEVEL=bogus" in text
        assert "MAX_LEARNT_CLAUSES=oops" in text
        assert "MAX_SNAPSHOTS=-5" in text
        assert "ALLOW_LPMBUILD_FALLBACK=false" in text

        assert config.OPT_LEVEL == "-O2"
        assert config.MAX_LEARNT_CLAUSES == 200
        assert config.MAX_SNAPSHOTS == 0
        assert config.INSTALL_PROMPT_DEFAULT == "n"
        assert config.ALLOW_LPMBUILD_FALLBACK is False

        conf = config.load_conf(target)
        assert conf["ALLOW_LPMBUILD_FALLBACK"] == "false"
    finally:
        config._apply_conf(original_conf)
