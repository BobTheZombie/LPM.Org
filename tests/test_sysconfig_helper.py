from __future__ import annotations

import stat
from pathlib import Path

from src.lpm.sysconfig import SysconfigResult, apply_system_configuration


def _stub_locale_generator(root: Path) -> SysconfigResult:
    return SysconfigResult(root / "usr/lib/locale/locale-archive", "skipped", "test stub")


def _result_map(results):
    return {res.path: res for res in results}


def test_apply_system_configuration_creates_expected_files(tmp_path: Path) -> None:
    results = apply_system_configuration(tmp_path, _locale_generator=_stub_locale_generator)
    paths = {res.path.relative_to(tmp_path) for res in results}

    expected = {
        Path("etc/profile"),
        Path("etc/profile.d/00-lpm-path.sh"),
        Path("etc/profile.d/dircolors.sh"),
        Path("etc/bash.bashrc"),
        Path("etc/skel/.bash_profile"),
        Path("etc/skel/.bashrc"),
        Path("etc/skel/.bash_logout"),
        Path("etc/DIR_COLORS"),
        Path("etc/vconsole.conf"),
        Path("etc/locale.gen"),
        Path("etc/locale.conf"),
        Path("etc/default/useradd"),
        Path("usr/local/sbin/adduser"),
        Path("usr/local/sbin/deluser"),
        Path("usr/lib/locale/locale-archive"),
    }

    assert expected == paths

    profile = (tmp_path / "etc/profile").read_text(encoding="utf-8")
    assert "lpm --sysconfig" in profile

    locale_gen = (tmp_path / "etc/locale.gen").read_text(encoding="utf-8")
    assert "en_US.UTF-8" in locale_gen

    adduser_mode = stat.S_IMODE((tmp_path / "usr/local/sbin/adduser").stat().st_mode)
    assert adduser_mode == 0o755


def test_apply_system_configuration_is_idempotent(tmp_path: Path) -> None:
    apply_system_configuration(tmp_path, _locale_generator=_stub_locale_generator)
    results = apply_system_configuration(tmp_path, _locale_generator=_stub_locale_generator)

    for res in results:
        relative = res.path.relative_to(tmp_path)
        if relative == Path("usr/lib/locale/locale-archive"):
            assert res.action == "skipped"
        else:
            assert res.action == "unchanged"


def test_apply_system_configuration_preserves_customisations(tmp_path: Path) -> None:
    apply_system_configuration(tmp_path, _locale_generator=_stub_locale_generator)
    profile = tmp_path / "etc/profile"
    profile.write_text("custom profile\n", encoding="utf-8")

    results = apply_system_configuration(tmp_path, _locale_generator=_stub_locale_generator)
    res_map = _result_map(results)

    assert res_map[profile].action == "skipped"
    assert "preserved" in res_map[profile].message


def test_generate_locales_handles_absent_binary(monkeypatch, tmp_path: Path) -> None:
    from src.lpm import sysconfig as sysconfig_mod

    monkeypatch.setattr(sysconfig_mod.shutil, "which", lambda _: None)

    result = sysconfig_mod._generate_locales(tmp_path)

    assert result.path == tmp_path / "usr/lib/locale/locale-archive"
    assert result.action == "skipped"
    assert "not available" in result.message


def test_generate_locales_reports_success(monkeypatch, tmp_path: Path) -> None:
    from types import SimpleNamespace

    from src.lpm import sysconfig as sysconfig_mod

    monkeypatch.setattr(sysconfig_mod.shutil, "which", lambda _: "/usr/bin/locale-gen")

    def fake_run(cmd, check, capture_output, text):
        assert "--root" in cmd[-1]
        (tmp_path / "usr/lib/locale").mkdir(parents=True, exist_ok=True)
        (tmp_path / "usr/lib/locale/locale-archive").write_bytes(b"")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(sysconfig_mod.subprocess, "run", fake_run)

    result = sysconfig_mod._generate_locales(tmp_path)

    assert result.action == "created"
    assert result.message == "locale archive generated"




def test_generated_bashrc_uses_literal_single_backslashes(tmp_path: Path) -> None:
    """Prevent escaped PS1 syntax from appearing literally in Konsole."""
    apply_system_configuration(tmp_path, _locale_generator=_stub_locale_generator)
    skel = (tmp_path / "etc/skel/.bashrc").read_text(encoding="utf-8")
    system = (tmp_path / "etc/bash.bashrc").read_text(encoding="utf-8")

    expected = r"PS1='\[\e[1;35m\]\u@\h\[\e[0m\]:\[\e[1;34m\]\w\[\e[0m\]\$ '"
    assert expected in skel
    assert expected in system
    assert r"PS1='\\[" not in skel
    assert "set -o errexit" not in skel
    assert "set -o noclobber" not in skel
    assert "set -o pipefail" not in skel


def test_generated_bashrc_repairs_legacy_prompt_and_preserves_custom(
    tmp_path: Path,
) -> None:
    import os
    import shutil
    import subprocess

    if not shutil.which("bash"):
        import pytest
        pytest.skip("bash executable unavailable")

    apply_system_configuration(tmp_path, _locale_generator=_stub_locale_generator)
    bashrc = tmp_path / "etc/bash.bashrc"
    script = r'''
        PS1="\\[\\e[1;35m\\]\\u@\\h"
        source "$1"
        [[ "$PS1" == "\[\e[1;35m\]"* ]] || exit 21
        [[ "$PS1" != *"\\["* ]] || exit 22
        PS1="user custom > "
        __lpm_repair_prompt
        [[ "$PS1" == "user custom > " ]] || exit 23
    '''
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-ic", script, "_", str(bashrc)],
        capture_output=True,
        text=True,
        env={**os.environ, "TERM": "xterm-256color"},
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
