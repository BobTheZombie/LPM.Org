import os
import shlex
import shutil
import subprocess
import sys
import textwrap
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if "zstandard" not in sys.modules:
    module = types.ModuleType("zstandard")

    class _Writer:
        def __init__(self, fh):
            self._fh = fh
            self._started = False

        def write(self, data):
            if not self._started:
                self._fh.write(b"\x28\xb5\x2f\xfd")
                self._started = True
            return self._fh.write(data)

        def flush(self):
            return self._fh.flush()

        def close(self):
            return None

        def __enter__(self):
            if not self._started:
                self._fh.write(b"\x28\xb5\x2f\xfd")
                self._started = True
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Compressor:
        def stream_writer(self, fh):
            return _Writer(fh)

    class _Reader:
        def __init__(self, fh):
            self._fh = fh
            self._skipped = False

        def read(self, size=-1):
            if not self._skipped:
                self._fh.read(4)
                self._skipped = True
            return self._fh.read(size)

        def close(self):
            return self._fh.close()

        def readable(self):
            return True

    class _Decompressor:
        def stream_reader(self, fh):
            return _Reader(fh)

    module.ZstdCompressor = _Compressor
    module.ZstdDecompressor = _Decompressor
    sys.modules["zstandard"] = module

if "tqdm" not in sys.modules:
    module = types.ModuleType("tqdm")

    class _DummyTqdm:
        def __init__(self, iterable=None, **kwargs):
            self.iterable = iterable or []
            self.n = 0
            self.total = kwargs.get("total")

        def __iter__(self):
            return iter(self.iterable)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def update(self, *args, **kwargs):
            return None

        def set_description(self, *args, **kwargs):
            return None

    module.tqdm = _DummyTqdm  # type: ignore[attr-defined]
    sys.modules["tqdm"] = module

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


def test_run_lpmbuild_wraps_named_install_script(tmp_path, monkeypatch):
    script = tmp_path / "foo.lpmbuild"
    script.write_text(
        "NAME=foo\nVERSION=1\ninstall=foo.install\n"
        "prepare(){ :; }\nbuild(){ :; }\ninstall(){ :; }\n"
    )

    install_source = tmp_path / "foo.install"
    install_source.write_text(
        textwrap.dedent(
            """
            post_install() {
                echo post-install
            }

            post_upgrade() {
                echo post-upgrade
            }
            """
        ).strip()
    )

    monkeypatch.setattr(lpm, "sandboxed_run", lambda *args, **kwargs: None)

    captured = {}

    def fake_build_package(stagedir, meta, out, sign=True):
        captured["stagedir"] = stagedir
        out.write_text("pkg")

    def fail_generate(_):
        raise AssertionError("generate_install_script should not run when install= is provided")

    monkeypatch.setattr(lpm, "build_package", fake_build_package)
    monkeypatch.setattr(lpm, "generate_install_script", fail_generate)

    lpm.run_lpmbuild(script, outdir=tmp_path, prompt_install=False, build_deps=False)

    install_sh = captured["stagedir"] / ".lpm-install.sh"
    data = install_sh.read_text()
    assert data.startswith("#!/bin/bash\nset -euo pipefail\n")
    assert "post_install \"$new_full\"" in data
    assert "post_upgrade \"$new_full\" \"$old_full\"" in data
    assert "post_install()" in data
    assert "post_upgrade()" in data
    assert os.access(install_sh, os.X_OK)
    shutil.rmtree(captured["stagedir"])


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


def test_run_lpmbuild_install_phase_allows_install_command(tmp_path, monkeypatch):
    script = tmp_path / "foo.lpmbuild"
    script.write_text(
        textwrap.dedent(
            """
            NAME=foo
            VERSION=1
            RELEASE=1
            ARCH=noarch
            prepare() {
                printf 'payload\n' > "$SRCROOT/message.txt"
            }
            build() { :; }
            install() {
                install -Dm644 "$SRCROOT/message.txt" "$pkgdir/usr/share/foo/message.txt"
            }
            """
        )
    )

    monkeypatch.setitem(lpm.CONF, "SANDBOX_MODE", "none")
    monkeypatch.setattr(lpm, "generate_install_script", lambda stagedir: ":")

    out_path, _, _, _ = lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    assert out_path.exists()
    _, manifest = lpm.read_package_meta(out_path)
    entry = next((item for item in manifest if item["path"] == "/usr/share/foo/message.txt"), None)
    assert entry is not None
    assert entry["size"] > 0

    tf = lpm.open_package_tar(out_path, stream=False)
    try:
        member = next(
            (
                m
                for m in tf.getmembers()
                if Path(m.name).as_posix().lstrip("./") == "usr/share/foo/message.txt"
            ),
            None,
        )
        assert member is not None
        with tf.extractfile(member) as fh:
            assert fh.read() == b"payload\n"
    finally:
        tf.close()

    for suffix in ("pkg-foo", "build-foo", "src-foo"):
        shutil.rmtree(Path(f"/tmp/{suffix}"), ignore_errors=True)


def test_run_lpmbuild_phase_wrapper_preserves_common_variable_names(tmp_path, monkeypatch):
    script = tmp_path / "phase-vars.lpmbuild"
    script.write_text(
        textwrap.dedent(
            r"""
            NAME=phase-vars
            VERSION=1

            log_phase_vars() {
                local label="$1"
                shift || true
                local log="$SRCROOT/var-check.log"
                for var in phase def new; do
                    eval "present=\${${var}+x}"
                    if [ "$present" = "x" ]; then
                        eval "value=\${${var}}"
                        printf '%s %s:%s\n' "$label" "$var" "$value" >> "$log"
                    else
                        printf '%s %s:unset\n' "$label" "$var" >> "$log"
                    fi
                done
            }

            prepare() {
                log_phase_vars prepare
                phase="prepare-phase"
                def="prepare-def"
                new="prepare-new"
            }

            build() {
                log_phase_vars build
                phase="build-phase"
                def="build-def"
                new="build-new"
            }

            install() {
                log_phase_vars install
                phase="install-phase"
                def="install-def"
                new="install-new"
            }
            """
        ).strip()
        + "\n"
    )

    monkeypatch.setitem(lpm.CONF, "SANDBOX_MODE", "none")
    monkeypatch.setattr(lpm, "generate_install_script", lambda stagedir: ":")

    def fake_build_package(stagedir, meta, out, sign=True):
        out.write_text("pkg")

    monkeypatch.setattr(lpm, "build_package", fake_build_package)

    lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    log_path = Path("/tmp/src-phase-vars/var-check.log")
    assert log_path.exists()
    assert log_path.read_text().splitlines() == [
        "prepare phase:unset",
        "prepare def:unset",
        "prepare new:unset",
        "build phase:unset",
        "build def:unset",
        "build new:unset",
        "install phase:unset",
        "install def:unset",
        "install new:unset",
    ]

    for suffix in ("pkg-phase-vars", "build-phase-vars", "src-phase-vars"):
        shutil.rmtree(Path(f"/tmp/{suffix}"), ignore_errors=True)


def test_run_lpmbuild_runs_phases_under_bwrap(tmp_path, monkeypatch):
    script_dir = tmp_path / "pkg"
    script_dir.mkdir()
    script = script_dir / "foo.lpmbuild"
    script.write_text(
        textwrap.dedent(
            """
            NAME=foo
            VERSION=1

            prepare() {
                echo prepare >> "$SRCROOT/phases.log"
            }

            build() {
                echo build >> "$SRCROOT/phases.log"
            }

            install() {
                echo install >> "$SRCROOT/phases.log"
            }
            """
        ).strip()
    )

    monkeypatch.setitem(lpm.CONF, "SANDBOX_MODE", "bwrap")
    monkeypatch.setattr(lpm, "generate_install_script", lambda stagedir: "echo hi")

    recorded = {}

    def fake_build_package(stagedir, meta, out, sign=True):
        recorded["stagedir"] = stagedir
        out.write_text("pkg")

    monkeypatch.setattr(lpm, "build_package", fake_build_package)

    real_run = subprocess.run
    last_srcroot = {}

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "bwrap":
            command_str = cmd[-1]
            env = kwargs.get("env") or {}
            srcroot = Path(env["SRCROOT"])
            last_srcroot["path"] = srcroot
            assert str(script.resolve()) in command_str
            adjusted = command_str.replace("cd /src", f"cd {shlex.quote(str(srcroot))}")
            return real_run(["bash", "-c", adjusted], *args, **kwargs)
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)

    out_path, _, _, _ = lpm.run_lpmbuild(
        script,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    assert out_path.exists()

    srcroot = last_srcroot.get("path")
    assert srcroot is not None
    phases_log = srcroot / "phases.log"
    assert phases_log.read_text().splitlines() == ["prepare", "build", "install"]

    shutil.rmtree(recorded["stagedir"], ignore_errors=True)
