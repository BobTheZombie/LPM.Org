import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lpm

def test_run_lpmbuild_generates_install_script(tmp_path, monkeypatch):
    script = tmp_path / "foo.lpmbuild"
    script.write_text(
        "NAME=foo\nVERSION=1\n\nprepare(){ :; }\nbuild(){ :; }\ninstall(){ :; }\n"
    )

    called = {}

    def fake_generate_install_script(stagedir):
        called['stagedir'] = stagedir
        return "echo generated"

    monkeypatch.setattr(lpm, "generate_install_script", fake_generate_install_script)
    monkeypatch.setattr(lpm, "sandboxed_run", lambda *args, **kwargs: None)

    def fake_build_package(stagedir, meta, out, sign=True):
        out.write_text("pkg")

    monkeypatch.setattr(lpm, "build_package", fake_build_package)

    lpm.run_lpmbuild(script, outdir=tmp_path, prompt_install=False, build_deps=False)

    install_sh = called['stagedir'] / ".lpm-install.sh"
    assert install_sh.read_text() == "#!/bin/sh\nset -e\necho generated\n"
    assert os.access(install_sh, os.X_OK)
    shutil.rmtree(called['stagedir'])


def test_run_lpmbuild_defaults_arch_to_noarch(tmp_path, monkeypatch):
    script = tmp_path / "foo.lpmbuild"
    script.write_text(
        "NAME=foo\nVERSION=1\nRELEASE=1\nprepare(){ :; }\nbuild(){ :; }\ninstall(){ :; }\n"
    )

    monkeypatch.setattr(lpm, "ARCH", "")
    monkeypatch.setattr(lpm, "generate_install_script", lambda stagedir: "echo hi")
    monkeypatch.setattr(lpm, "sandboxed_run", lambda *args, **kwargs: None)

    recorded = {}

    def fake_build_package(stagedir, meta, out, sign=True):
        recorded["meta_arch"] = meta.arch
        recorded["stagedir"] = stagedir
        recorded["out"] = out
        out.write_text("pkg")

    monkeypatch.setattr(lpm, "build_package", fake_build_package)

    out_path, _, _, splits = lpm.run_lpmbuild(script, outdir=tmp_path, prompt_install=False, build_deps=False)

    assert recorded["meta_arch"] == lpm.PkgMeta.__dataclass_fields__["arch"].default
    assert out_path.name.endswith(".zst")
    assert ".." not in out_path.name
    assert splits == []

    shutil.rmtree(recorded["stagedir"])
