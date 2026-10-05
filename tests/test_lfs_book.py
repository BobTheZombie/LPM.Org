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


def test_content_ids_do_not_split_a_book_section(tmp_path: Path) -> None:
    book = tmp_path / "book.html"
    book.write_text(
        "<!doctype html><html><body>"
        '<h2><a id="ch-system-zlib"></a>8.6 Zlib-1.3.2</h2>'
        '<pre class="userinput"><kbd class="command">./configure --prefix=/usr</kbd></pre>'
        '<h3 id="id6927">8.6.1 Installation of Zlib</h3>'
        '<div id="id6855">generated content anchor</div>'
        '<pre class="userinput"><kbd class="command">make</kbd></pre>'
        '<pre id="id6856" class="userinput"><kbd class="command">make install</kbd></pre>'
        "</body></html>",
        encoding="utf-8",
    )

    result = lfs_book.extract_build_instructions(book, tmp_path / "instructions")
    plan = json.loads(Path(result["phase_plan"]).read_text(encoding="utf-8"))

    assert plan["section_count"] == 1
    section = plan["sections"][0]
    assert section["section"] == "ch-system-zlib"
    assert section["phase"] == "final-system"
    assert section["commands"] == [
        "./configure --prefix=/usr",
        "make",
        "make install",
    ]


def test_bindmount_section_is_owned_by_orchestrator() -> None:
    assert lfs_book._context_for("ch-tools-bindmount", 7, "chroot") == "host-root"
    automatic, reason = lfs_book._automation_policy("ch-tools-bindmount", 7)
    assert automatic is False
    assert "managed by lpm" in reason


def test_package_management_examples_are_not_executable() -> None:
    for section in ("pkgmgmt-upgrade-issues", "ch-system-pkgmgt"):
        automatic, reason = lfs_book._automation_policy(section, 8)
        assert automatic is False
        assert "documentation" in reason


def test_network_configuration_is_installer_owned() -> None:
    automatic, reason = lfs_book._automation_policy("ch-config-network", 9)
    assert automatic is False
    assert "machine-specific" in reason


def test_fstab_configuration_is_installer_owned() -> None:
    automatic, reason = lfs_book._automation_policy("ch-bootable-fstab", 10)
    assert automatic is False
    assert "machine-specific" in reason


def test_grub_configuration_is_installer_owned() -> None:
    automatic, reason = lfs_book._automation_policy("ch-bootable-grub", 10)
    assert automatic is False
    assert "machine-specific" in reason


def test_manual_template_placeholders_do_not_block_plan_extraction(
    tmp_path: Path,
) -> None:
    book = tmp_path / "book.html"
    book.write_text(
        "<!doctype html><html><body>"
        '<h2 id="ch-config-network">9.5 Network Configuration</h2>'
        '<pre class="userinput"><kbd class="command">'
        'cat &gt; /etc/hosts &lt;&lt; "EOF"\n'
        '&lt;192.168.0.2&gt; &lt;FQDN&gt; [alias] ...\nEOF'
        "</kbd></pre></body></html>",
        encoding="utf-8",
    )

    result = lfs_book.extract_build_instructions(book, tmp_path / "instructions")
    plan = json.loads(Path(result["phase_plan"]).read_text(encoding="utf-8"))
    section = plan["sections"][0]

    assert section["automatic"] is False
    assert "machine-specific" in section["reason"]
    assert "<FQDN>" in section["commands"][0]


def test_fstab_placeholders_do_not_block_plan_extraction(tmp_path: Path) -> None:
    book = tmp_path / "book.html"
    book.write_text(
        "<!doctype html><html><body>"
        '<h2 id="ch-bootable-fstab">10.2 Creating /etc/fstab</h2>'
        '<pre class="userinput"><kbd class="command">'
        'cat &gt; /etc/fstab &lt;&lt; "EOF"\n'
        '/dev/&lt;xxx&gt; / &lt;fff&gt; defaults 1 1\nEOF'
        "</kbd></pre></body></html>",
        encoding="utf-8",
    )

    result = lfs_book.extract_build_instructions(book, tmp_path / "instructions")
    plan = json.loads(Path(result["phase_plan"]).read_text(encoding="utf-8"))
    section = plan["sections"][0]

    assert section["automatic"] is False
    assert "machine-specific" in section["reason"]
    assert "/dev/<xxx>" in section["commands"][0]


def test_grub_placeholders_do_not_block_plan_extraction(tmp_path: Path) -> None:
    book = tmp_path / "book.html"
    book.write_text(
        "<!doctype html><html><body>"
        '<h2 id="ch-bootable-grub">10.4 Using GRUB</h2>'
        '<pre class="userinput"><kbd class="command">'
        'efibootmgr -c -d /dev/sd&lt;x&gt; -p &lt;y&gt; '
        '-L "LFS" -l \'\\EFI\\BOOT\\BOOT&lt;X64&gt;.EFI\''
        "</kbd></pre></body></html>",
        encoding="utf-8",
    )

    result = lfs_book.extract_build_instructions(book, tmp_path / "instructions")
    plan = json.loads(Path(result["phase_plan"]).read_text(encoding="utf-8"))
    section = plan["sections"][0]

    assert section["automatic"] is False
    assert "machine-specific" in section["reason"]
    assert "/dev/sd<x>" in section["commands"][0]


def test_glibc_check_is_omitted_from_automatic_bootstrap() -> None:
    assert lfs_book._normalize_automatic_command(
        "make check", "ch-system-glibc"
    ) == ""


def test_glibc_optional_and_upgrade_commands_are_omitted() -> None:
    omitted = (
        'grep "Timed out" $(find -name \\*.out)',
        "rm -f /usr/sbin/nscd",
        "systemctl disable --now nscd",
        "make DESTDIR=$PWD/dest install\ninstall -vm755 dest/usr/lib/*.so.* /usr/lib",
        "DIR=$(dirname $(gcc -print-libgcc-file-name))\nrm -rfv $DIR/include-fixed/*",
        "make localedata/install-locales",
        "tzselect",
        "ln -sfv /usr/share/zoneinfo/<xxx> /etc/localtime",
    )

    for command in omitted:
        assert lfs_book._normalize_automatic_command(
            command, "ch-system-glibc"
        ) == ""


def test_glibc_clean_install_commands_remain() -> None:
    for command in (
        "touch /etc/ld.so.conf",
        "make install",
        "localedef -i C -f UTF-8 C.UTF-8",
    ):
        assert lfs_book._normalize_automatic_command(
            command, "ch-system-glibc"
        ) == command


def test_glibc_build_prefix_is_resume_safe() -> None:
    patch = lfs_book._normalize_automatic_command(
        "patch -Np1 -i ../glibc-fhs-1.patch", "ch-system-glibc"
    )
    configure = lfs_book._normalize_automatic_command(
        "../configure --prefix=/usr", "ch-system-glibc"
    )
    build = lfs_book._normalize_automatic_command(
        "make", "ch-system-glibc"
    )

    assert "if [ ! -e build/libc.so ]" in patch
    assert "if [ ! -f Makefile ]" in configure
    assert "if [ ! -e libc.so ]" in build
    assert lfs_book._normalize_automatic_command(
        "mkdir -v build\ncd       build", "ch-system-glibc"
    ) == "mkdir -pv build\ncd build"


def test_non_glibc_check_remains_fatal() -> None:
    assert lfs_book._normalize_automatic_command(
        "make check", "ch-system-zlib"
    ) == ""


def test_package_test_suites_and_reports_are_globally_omitted() -> None:
    for command in (
        "make check",
        "make -k check",
        "make test",
        "make tests",
        'su tester -c "PATH=$PATH make -k check"',
        "ninja -C build test",
        "meson test -C build",
        "ctest --test-dir build",
        "python3 -m pytest -q",
        "python3 -m test -j6",
        "cargo test --locked",
        "go test ./...",
        "prove -j6 tests",
        "python3 run_tests.py",
        "grep '^FAIL:' $(find -name '*.log')",
        'grep "Timed out" $(find -name \\*.out)',
        "cat $(find -name '*.log') | grep -c ^PASS",
    ):
        for section in ("ch-tools-gcc-pass1", "ch-system-binutils"):
            assert lfs_book._normalize_automatic_command(
                command, section
            ) == ""


def test_non_test_make_target_remains_executable() -> None:
    assert lfs_book._normalize_automatic_command(
        "make tooldir=/usr", "ch-system-binutils"
    ) == "make tooldir=/usr"


def test_util_linux_standalone_tests_and_setup_are_omitted() -> None:
    for command in (
        "bash tests/run.sh --srcdir=$PWD --builddir=$PWD",
        "touch /etc/fstab",
        "chown -R tester .",
        'su tester -c "make -k check"',
    ):
        assert lfs_book._normalize_automatic_command(
            command, "ch-system-util-linux"
        ) == ""


def test_gmp_alternate_abi_example_is_omitted() -> None:
    assert lfs_book._normalize_automatic_command(
        "ABI=32 ./configure ...", "ch-system-gmp"
    ) == ""


def test_groff_paper_size_defaults_to_us_letter() -> None:
    assert lfs_book._normalize_automatic_command(
        "PAGE=<paper_size> ./configure --prefix=/usr", "ch-system-groff"
    ) == "PAGE=letter ./configure --prefix=/usr"


def test_locale_configuration_defaults_to_us_utf8() -> None:
    command = (
        'cat > /etc/locale.conf << "EOF"\n'
        "LANG=<ll>_<CC>.<charmap><@modifiers>\n"
        "EOF"
    )
    assert lfs_book._normalize_automatic_command(
        command, "ch-config-locale"
    ) == command.replace(
        "<ll>_<CC>.<charmap><@modifiers>",
        "en_US.UTF-8",
    )
    assert lfs_book._normalize_automatic_command(
        'localectl set-locale LANG="<ll>_<CC>.<charmap><@modifiers>"',
        "ch-config-locale",
    ) == 'localectl set-locale LANG="en_US.UTF-8"'


def test_grub_build_is_x86_64_efi_only() -> None:
    command = (
        "./configure --prefix=/usr \\\n"
        "  --sysconfdir=/etc \\\n"
        "  --disable-efiemu"
    )
    normalized = lfs_book._normalize_automatic_command(
        command, "ch-system-grub"
    )
    assert normalized.startswith(
        "./configure --with-platform=efi --target=x86_64 "
    )
    assert normalized.count("--target=x86_64") == 1
    assert normalized.count("--with-platform=efi") == 1


def test_unresolved_book_placeholder_is_rejected() -> None:
    for command in ("./configure --host=...", "PAGE=<choice> ./configure"):
        try:
            lfs_book._normalize_automatic_command(command, "ch-system-example")
        except ValueError as error:
            assert "unresolved LFS command placeholder" in str(error)
        else:
            raise AssertionError("unresolved placeholder was accepted")


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


def test_verbose_mkdir_commands_are_replay_safe() -> None:
    assert lfs_book._normalize_automatic_command(
        "mkdir -v build\ncd build", "ch-system-binutils"
    ) == "mkdir -vp build\ncd build"


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
