from __future__ import annotations

import hashlib
import json
from pathlib import Path

from lpm import lfs_book


BOOK_HTML = b"""<!doctype html>
<html><body>
<h2 id="chapter05-binutils-pass1">5.2 Binutils Pass 1</h2>
<p>Prepare Binutils:</p>
<pre class="userinput"><kbd class="command">mkdir -v build
cd build
../configure --prefix=$LFS/tools</kbd></pre>
<pre class="screen">This output must not become a script.</pre>
<h2 id="chapter05-gcc-pass1">5.3 GCC Pass 1</h2>
<pre class="userinput"><kbd class="command">make -j$(nproc)
make install</kbd></pre>
</body></html>
"""


def test_extract_build_instructions_creates_numbered_scripts(tmp_path: Path) -> None:
    book = tmp_path / "book.html"
    book.write_bytes(BOOK_HTML)

    result = lfs_book.extract_build_instructions(book, tmp_path / "instructions")

    assert result["instruction_count"] == 2
    index = json.loads(Path(result["index"]).read_text(encoding="utf-8"))
    assert [item["section"] for item in index["instructions"]] == [
        "chapter05-binutils-pass1",
        "chapter05-gcc-pass1",
    ]
    first = Path(index["instructions"][0]["script"])
    assert first.stat().st_mode & 0o111
    assert "../configure --prefix=$LFS/tools" in first.read_text(encoding="utf-8")
    assert "This output must not become a script" not in first.read_text(encoding="utf-8")


def test_cache_book_reuses_verified_cached_copy(tmp_path: Path, monkeypatch) -> None:
    downloads: list[str] = []

    def fake_download(url: str, destination: Path) -> None:
        downloads.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.name.endswith(".html"):
            destination.write_bytes(BOOK_HTML)
        else:
            destination.write_text(f"cached {destination.name}\n", encoding="utf-8")

    monkeypatch.setattr(lfs_book, "_download", fake_download)
    digest = hashlib.sha256(BOOK_HTML).hexdigest()
    first = lfs_book.cache_book(
        version="13.1-systemd",
        cache_dir=tmp_path / "cache",
        expected_sha256=digest,
    )
    assert len(downloads) == 3

    downloads.clear()
    second = lfs_book.cache_book(
        version="13.1-systemd",
        cache_dir=tmp_path / "cache",
        expected_sha256=digest,
        offline=True,
    )
    assert downloads == []
    assert first["sha256"] == second["sha256"] == digest


def test_cache_book_rejects_checksum_mismatch(tmp_path: Path, monkeypatch) -> None:
    def fake_download(_url: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(BOOK_HTML)

    monkeypatch.setattr(lfs_book, "_download", fake_download)

    try:
        lfs_book.cache_book(
            version="13.1-systemd",
            cache_dir=tmp_path / "cache",
            expected_sha256="0" * 64,
        )
    except RuntimeError as exc:
        assert "checksum mismatch" in str(exc)
    else:
        raise AssertionError("checksum mismatch was accepted")
