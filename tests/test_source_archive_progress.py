"""Progress and extraction security regressions for buildpkg source archives."""

import io
import os
import tarfile

import pytest

from lpm import app


class RecordingProgress:
    def __init__(self, **kwargs):
        self.total = kwargs["total"]
        self.n = 0
        self.updates = []
        self.unit = kwargs["unit"]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def update(self, amount):
        self.updates.append(amount)
        self.n += amount


@pytest.fixture
def archive_bars(monkeypatch):
    bars = []

    def recording_progress(_iterable, **kwargs):
        bar = RecordingProgress(**kwargs)
        bars.append(bar)
        return bar

    monkeypatch.setattr(app, "progress_bar", recording_progress)
    return bars


@pytest.mark.parametrize("compression", ["gz", "xz", "bz2", "tar"])
def test_build_source_extraction_shows_live_input_byte_progress(
    tmp_path, archive_bars, compression
):
    payload = os.urandom(320 * 1024)
    archive = tmp_path / (
        "release.tar" if compression == "tar" else f"release.tar.{compression}"
    )
    mode = "w" if compression == "tar" else f"w:{compression}"
    with tarfile.open(archive, mode) as tf:
        folder = tarfile.TarInfo("demo-1/")
        folder.type = tarfile.DIRTYPE
        tf.addfile(folder)
        item = tarfile.TarInfo("demo-1/nested/payload.bin")
        item.size = len(payload)
        tf.addfile(item, io.BytesIO(payload))
        link = tarfile.TarInfo("demo-1/nested/linked.bin")
        link.type = tarfile.LNKTYPE
        link.linkname = "demo-1/nested/payload.bin"
        tf.addfile(link)

    dest = tmp_path / "extracted"
    dest.mkdir()
    app._extract_lpmbuild_archive(archive, dest)

    assert (dest / "nested" / "payload.bin").read_bytes() == payload
    assert (dest / "nested" / "linked.bin").read_bytes() == payload
    assert not (dest / "demo-1").exists()
    assert len(archive_bars) == 1
    bar = archive_bars[0]
    assert bar.unit == "B"
    assert bar.total == archive.stat().st_size
    assert bar.n == bar.total
    assert len(bar.updates) >= 2


def test_build_source_zstd_extracts_without_reading_entire_archive(
    tmp_path, archive_bars
):
    zstandard = pytest.importorskip("zstandard")
    payload = os.urandom(256 * 1024)
    archive = tmp_path / "release.tar.zst"
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w") as tf:
        member = tarfile.TarInfo("demo-1/file.bin")
        member.size = len(payload)
        tf.addfile(member, io.BytesIO(payload))
    archive.write_bytes(zstandard.ZstdCompressor().compress(tar_bytes.getvalue()))

    dest = tmp_path / "extracted"
    dest.mkdir()
    app._extract_lpmbuild_archive(archive, dest)

    assert (dest / "file.bin").read_bytes() == payload
    assert len(archive_bars) == 1
    assert archive_bars[0].total == archive.stat().st_size
    assert archive_bars[0].n == archive_bars[0].total


def test_build_source_extraction_rejects_path_traversal_without_unsafe_retry(
    tmp_path, archive_bars, monkeypatch
):
    archive = tmp_path / "malicious.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        member = tarfile.TarInfo("project/../../../escape.txt")
        payload = b"not outside"
        member.size = len(payload)
        tf.addfile(member, io.BytesIO(payload))

    def unexpected_external_tar(*_args, **_kwargs):
        raise AssertionError("must not retry rejected archives with GNU tar")

    monkeypatch.setattr(app.subprocess, "run", unexpected_external_tar)
    dest = tmp_path / "src"
    dest.mkdir()
    with pytest.raises(SystemExit):
        app._extract_lpmbuild_archive(archive, dest)
    assert not (tmp_path / "escape.txt").exists()
