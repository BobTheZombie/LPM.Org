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

ANCHOR_ID_BOOK_HTML = b"""<!doctype html>
<html><body>
<h2><a id="ch-tools-binutils-pass1"></a>5.2. Binutils-2.47 - Pass 1</h2>
<h3>5.2.1. Installation of Cross Binutils</h3>
<pre class="userinput"><kbd class="command">mkdir -v build</kbd></pre>
<h3>Note</h3>
<pre class="userinput"><kbd class="command">make</kbd></pre>
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
    plan = json.loads(Path(result["phase_plan"]).read_text(encoding="utf-8"))
    assert plan["section_count"] == 2
    assert plan["sections"][0]["phase"] == "cross-toolchain"
    assert plan["sections"][0]["context"] == "lfs-user"


def test_extract_tracks_anchor_ids_and_keeps_numbered_heading(tmp_path: Path) -> None:
    book = tmp_path / "book.html"
    book.write_bytes(ANCHOR_ID_BOOK_HTML)
    result = lfs_book.extract_build_instructions(book, tmp_path / "instructions")
    plan = json.loads(Path(result["phase_plan"]).read_text(encoding="utf-8"))

    assert plan["section_count"] == 1
    section = plan["sections"][0]
    assert section["section"] == "ch-tools-binutils-pass1"
    assert section["title"] == "5.2.1. Installation of Cross Binutils"
    assert section["chapter"] == 5
    assert section["phase"] == "cross-toolchain"
    assert section["commands"] == ["mkdir -v build", "make"]


def test_bindmount_section_is_owned_by_orchestrator() -> None:
    assert lfs_book._context_for("ch-tools-bindmount", 7, "chroot") == "host-root"
    automatic, reason = lfs_book._automation_policy("ch-tools-bindmount", 7)
    assert automatic is False
    assert "managed by lpm" in reason


def test_interactive_shell_refresh_is_removed_without_losing_following_commands(
    tmp_path: Path,
) -> None:
    book = tmp_path / "book.html"
    book.write_text(
        "<!doctype html><html><body>"
        '<h2 id="ch-tools-createfiles">7.6 Creating Essential Files</h2>'
        '<pre class="userinput"><kbd class="command">echo passwd &gt; /etc/passwd</kbd></pre>'
        '<pre class="userinput"><kbd class="command">exec /usr/bin/bash --login</kbd></pre>'
        '<pre class="userinput"><kbd class="command">touch /var/log/lastlog</kbd></pre>'
        "</body></html>",
        encoding="utf-8",
    )

    result = lfs_book.extract_build_instructions(book, tmp_path / "instructions")
    plan = json.loads(Path(result["phase_plan"]).read_text(encoding="utf-8"))
    section = plan["sections"][0]
    script = Path(section["script"]).read_text(encoding="utf-8")

    assert section["commands"] == [
        "echo passwd > /etc/passwd",
        "touch /var/log/lastlog",
    ]
    assert "exec /usr/bin/bash --login" not in script
    assert "touch /var/log/lastlog" in script


def test_symbolic_link_commands_are_replay_safe(tmp_path: Path) -> None:
    book = tmp_path / "book.html"
    book.write_text(
        "<!doctype html><html><body>"
        '<h2 id="ch-tools-createfiles">7.6 Creating Essential Files</h2>'
        '<pre class="userinput"><kbd class="command">'
        "ln -sv /proc/self/mounts /etc/mtab"
        "</kbd></pre></body></html>",
        encoding="utf-8",
    )

    result = lfs_book.extract_build_instructions(book, tmp_path / "instructions")
    plan = json.loads(Path(result["phase_plan"]).read_text(encoding="utf-8"))
    command = plan["sections"][0]["commands"][0]

    assert command == "ln -svfn /proc/self/mounts /etc/mtab"


def test_cache_book_reuses_verified_cached_copy(tmp_path: Path, monkeypatch) -> None:
    assert lfs_book.default_support_url("13.1-systemd", "wget-list") == (
        "https://www.linuxfromscratch.org/lfs/view/13.1-systemd/"
        "wget-list-systemd"
    )
    assert lfs_book.default_support_url("13.1-systemd", "md5sums") == (
        "https://www.linuxfromscratch.org/lfs/view/13.1-systemd/md5sums"
    )

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
    assert downloads[1].endswith("/wget-list-systemd")
    assert downloads[2].endswith("/md5sums")

    downloads.clear()
    second = lfs_book.cache_book(
        version="13.1-systemd",
        cache_dir=tmp_path / "cache",
        expected_sha256=digest,
        offline=True,
    )
    assert downloads == []
    assert first["sha256"] == second["sha256"] == digest


def test_cache_book_replaces_legacy_sysv_wget_list(tmp_path: Path, monkeypatch) -> None:
    downloads: list[str] = []

    def fake_download(url: str, destination: Path) -> None:
        downloads.append(url)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(BOOK_HTML if destination.suffix == ".html" else url.encode())

    monkeypatch.setattr(lfs_book, "_download", fake_download)
    cache = tmp_path / "cache" / "13.1-systemd"
    cache.mkdir(parents=True)
    book = cache / "LFS-BOOK-13.1-NOCHUNKS.html"
    book.write_bytes(BOOK_HTML)
    legacy = cache / "wget-list"
    legacy.write_text("legacy sysv list\n", encoding="utf-8")
    sums = cache / "md5sums"
    sums.write_text("old sums\n", encoding="utf-8")
    (cache / "cache.json").write_text(json.dumps({
        "version": "13.1-systemd",
        "url": lfs_book.default_book_url("13.1-systemd"),
        "sha256": hashlib.sha256(BOOK_HTML).hexdigest(),
        "support": {
            "wget-list": {
                "url": lfs_book.default_support_url("13.1-systemd", "wget-list").replace("wget-list-systemd", "wget-list"),
                "sha256": hashlib.sha256(legacy.read_bytes()).hexdigest(),
            },
            "md5sums": {
                "url": lfs_book.default_support_url("13.1-systemd", "md5sums"),
                "sha256": hashlib.sha256(sums.read_bytes()).hexdigest(),
            },
        },
    }), encoding="utf-8")

    lfs_book.cache_book(version="13.1-systemd", cache_dir=tmp_path / "cache")
    assert downloads == [
        lfs_book.default_support_url("13.1-systemd", "wget-list")
    ]
    assert legacy.read_bytes().endswith(b"wget-list-systemd")


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
