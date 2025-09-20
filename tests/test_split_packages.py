import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import lpm


def test_run_lpmbuild_creates_split_packages(tmp_path, monkeypatch):
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))

    script = tmp_path / "split.lpmbuild"
    script.write_text(
        "\n".join(
            [
                "NAME=foo",
                "VERSION=1.2.3",
                "RELEASE=2",
                "ARCH=noarch",
                "SUMMARY=\"Base package\"",
                "prepare(){ :; }",
                "build(){ :; }",
                "install(){",
                "  mkdir -p \"$pkgdir/usr/bin\"",
                "  echo base > \"$pkgdir/usr/bin/foo\"",
                "  split_a=\"$BUILDROOT/split-a\"",
                "  mkdir -p \"$split_a/usr/bin\"",
                "  echo alpha > \"$split_a/usr/bin/foo-alpha\"",
                "  $LPM_SPLIT_PACKAGE --stagedir \"$split_a\" --name foo-alpha --summary 'Alpha compiler' --requires bar",
                "  split_b=\"$BUILDROOT/split-b\"",
                "  mkdir -p \"$split_b/usr/bin\"",
                "  echo beta > \"$split_b/usr/bin/foo-beta\"",
                "  $LPM_SPLIT_PACKAGE --stagedir \"$split_b\" --name foo-beta --provides foo-beta-bin",
                "}",
            ]
        )
    )

    out_path, _, _, splits = lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    assert out_path.exists()
    assert len(splits) == 2
    names = sorted(meta.name for _, meta in splits)
    assert names == ["foo-alpha", "foo-beta"]
    for path, meta in splits:
        assert path.exists()
        assert meta.version == "1.2.3"
        assert meta.release == "2"
        if meta.name == "foo-alpha":
            assert meta.requires == ["bar"]
            assert meta.summary == "Alpha compiler"
        if meta.name == "foo-beta":
            assert meta.provides == ["foo-beta-bin"]
