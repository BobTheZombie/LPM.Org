from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lpm
from lpm import PkgMeta


def test_cmd_buildpkg_shows_progress_and_summary(monkeypatch, tmp_path, capsys):
    script = tmp_path / "dummy.lpmbuild"
    script.write_text("")

    def fake_run_lpmbuild(script, outdir=None, **kwargs):
        for i, phase in enumerate(["prepare", "build", "install"], start=1):
            print(f"[{i}/3] {phase}", file=sys.stderr)
        out = (outdir or script.parent) / "foo-1-1.noarch.zst"
        out.write_text("dummy")
        return out, 1.0, 3

    def fake_read_package_meta(path):
        meta = PkgMeta(name="foo", version="1", release="1", arch="noarch", summary="demo")
        return meta, []

    monkeypatch.setattr(lpm, "run_lpmbuild", fake_run_lpmbuild)
    monkeypatch.setattr(lpm, "read_package_meta", fake_read_package_meta)

    args = SimpleNamespace(script=script, outdir=tmp_path, no_deps=False, install_default=None)
    lpm.cmd_buildpkg(args)

    captured = capsys.readouterr()
    assert "[1/3] prepare" in captured.err
    assert "[2/3] build" in captured.err
    assert "[3/3] install" in captured.err
    assert "Summary" in captured.out
    assert "Name         foo" in captured.out
