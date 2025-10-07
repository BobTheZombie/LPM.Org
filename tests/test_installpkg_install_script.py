import dataclasses
import importlib
import json
import shutil
import sqlite3
import sys
import tarfile
import textwrap
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _import_lpm(tmp_path, monkeypatch):
    monkeypatch.setenv("LPM_STATE_DIR", str(tmp_path / "state"))
    if "tqdm" not in sys.modules:
        module = types.ModuleType("tqdm")

        class _DummyTqdm:
            def __init__(self, iterable=None, **kwargs):
                self.iterable = iterable
                self.n = 0
                self.total = kwargs.get("total")

            def __iter__(self):
                return iter(self.iterable or [])

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def update(self, *args, **kwargs):
                return None

            def set_description(self, *args, **kwargs):
                return None

        module.tqdm = _DummyTqdm  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "tqdm", module)
    for mod in ["lpm", "src.config"]:
        if mod in sys.modules:
            del sys.modules[mod]
    return importlib.import_module("lpm")


def _make_install_script_pkg(lpm, tmp_path):
    staged = tmp_path / "stage-install-script"
    staged.mkdir()

    payload = staged / "foo"
    payload.write_text("from package\n")

    install_sh = staged / ".lpm-install.sh"
    install_sh.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        ": \"${LPM_ROOT:?}\"\n"
        "printf 'from script' > \"$LPM_ROOT/foo\"\n"
    )
    install_sh.chmod(0o755)

    manifest = lpm.collect_manifest(staged)
    meta = lpm.PkgMeta(name="scripted", version="1", release="1", arch="noarch")

    (staged / ".lpm-meta.json").write_text(json.dumps(dataclasses.asdict(meta)))
    (staged / ".lpm-manifest.json").write_text(json.dumps(manifest))

    out = tmp_path / "scripted.zst"
    with out.open("wb") as f:
        cctx = lpm.zstd.ZstdCompressor()
        with cctx.stream_writer(f) as compressor:
            with tarfile.open(fileobj=compressor, mode="w|") as tf:
                for p in staged.iterdir():
                    tf.add(p, arcname=p.name)

    shutil.rmtree(staged)
    return out


def _make_simple_pkg(lpm, tmp_path, *, name="scripted", version="1", release="1", payload="from package\n"):
    staged = tmp_path / f"stage-{name}-{version}-{release}"
    staged.mkdir()

    payload_path = staged / "foo"
    payload_path.write_text(payload)

    manifest = lpm.collect_manifest(staged)
    meta = lpm.PkgMeta(name=name, version=version, release=release, arch="noarch")

    (staged / ".lpm-meta.json").write_text(json.dumps(dataclasses.asdict(meta)))
    (staged / ".lpm-manifest.json").write_text(json.dumps(manifest))

    out = tmp_path / f"{name}-{version}-{release}.zst"
    with out.open("wb") as f:
        cctx = lpm.zstd.ZstdCompressor()
        with cctx.stream_writer(f) as compressor:
            with tarfile.open(fileobj=compressor, mode="w|") as tf:
                for p in staged.iterdir():
                    tf.add(p, arcname=p.name)

    shutil.rmtree(staged)
    return out


def _build_pkg_from_lpmbuild(lpm, tmp_path, monkeypatch, *, name, version, release, install_body):
    workdir = tmp_path / f"{name}-{version}-{release}-src"
    workdir.mkdir()

    script_path = workdir / f"{name}.lpmbuild"
    script_path.write_text(
        textwrap.dedent(
            f"""
            NAME={name}
            VERSION={version}
            RELEASE={release}
            install=foo.install
            prepare(){{ :; }}
            build(){{ :; }}
            staging(){{ :; }}
            """
        ).strip()
        + "\n"
    )

    install_path = workdir / "foo.install"
    install_path.write_text(install_body.strip() + "\n")

    payload_contents = "payload\n"

    def fake_sandboxed_run(
        func,
        cwd,
        env,
        script_path_arg,
        stagedir,
        buildroot,
        srcroot,
        *,
        aliases=(),
    ):
        if func == "staging" or "install" in aliases:
            target = stagedir / "installed.txt"
            target.write_text(payload_contents)

    def fail_generate(_stagedir):
        raise AssertionError("generate_install_script should not run when install= is provided")

    def fake_build_package(stagedir, meta, out, sign=True):
        manifest = lpm.collect_manifest(stagedir)
        (stagedir / ".lpm-meta.json").write_text(json.dumps(dataclasses.asdict(meta)))
        (stagedir / ".lpm-manifest.json").write_text(json.dumps(manifest))
        with out.open("wb") as f:
            cctx = lpm.zstd.ZstdCompressor()
            with cctx.stream_writer(f) as compressor:
                with tarfile.open(fileobj=compressor, mode="w|") as tf:
                    for path in sorted(stagedir.rglob("*")):
                        tf.add(path, arcname=str(path.relative_to(stagedir)))

    monkeypatch.setattr(lpm, "sandboxed_run", fake_sandboxed_run)
    monkeypatch.setattr(lpm, "generate_install_script", fail_generate)
    monkeypatch.setattr(lpm, "build_package", fake_build_package)

    pkg_path, *_ = lpm.run_lpmbuild(
        script_path,
        outdir=tmp_path,
        prompt_install=False,
        build_deps=False,
    )

    shutil.rmtree(workdir)
    return pkg_path


def test_installpkg_runs_embedded_script(tmp_path, monkeypatch):
    lpm = _import_lpm(tmp_path, monkeypatch)
    root = tmp_path / "root"
    root.mkdir()

    pkg = _make_install_script_pkg(lpm, tmp_path)

    lpm.installpkg(pkg, root=root, dry_run=False, verify=False, force=False, explicit=True)

    installed_payload = root / "foo"
    assert installed_payload.read_text() == "from script"
    assert not (root / ".lpm-install.sh").exists()

    conn = sqlite3.connect(tmp_path / "state" / "state.db")
    try:
        row = conn.execute(
            "SELECT manifest FROM installed WHERE name=?", ("scripted",)
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    manifest = json.loads(row[0])
    assert all(entry["path"] != "/.lpm-install.sh" for entry in manifest)


def test_post_upgrade_hook_runs_with_previous_version(tmp_path, monkeypatch):
    lpm = _import_lpm(tmp_path, monkeypatch)
    root = tmp_path / "root"
    root.mkdir()

    hook_dir = tmp_path / "hooks"
    hook_dir.mkdir()
    log = tmp_path / "upgrade.log"
    script = hook_dir / "post_upgrade"
    script.write_text(
        "#!/bin/sh\n"
        "[ -z \"$LPM_PREVIOUS_VERSION\" ] && exit 0\n"
        f"printf '%s %s %s %s\\n' \"$LPM_PKG\" \"$LPM_VERSION\" \"$LPM_PREVIOUS_VERSION\" \"$LPM_PREVIOUS_RELEASE\" >> {log}\n"
    )
    script.chmod(0o755)
    monkeypatch.setattr(lpm, "HOOK_DIR", hook_dir)

    pkg_v1 = _make_simple_pkg(lpm, tmp_path, name="sample", version="1.0", release="1")
    pkg_v2 = _make_simple_pkg(lpm, tmp_path, name="sample", version="2.0", release="3")

    lpm.installpkg(pkg_v1, root=root, dry_run=False, verify=False, force=False, explicit=True)
    assert not log.exists() or log.read_text().strip() == ""

    lpm.installpkg(pkg_v2, root=root, dry_run=False, verify=False, force=False, explicit=True)

    lines = log.read_text().splitlines()
    assert lines == ["sample 2.0 1.0 1"]


def test_installpkg_lpmbuild_install_hooks(tmp_path, monkeypatch):
    lpm = _import_lpm(tmp_path, monkeypatch)
    root = tmp_path / "root"
    root.mkdir()

    install_body = textwrap.dedent(
        """
        post_install() {
            printf 'post_install %s %s\n' "$1" "${LPM_INSTALL_ACTION:-}" >> "$LPM_ROOT/hook.log"
        }

        post_upgrade() {
            printf 'post_upgrade %s %s\n' "$1" "$2" >> "$LPM_ROOT/hook.log"
        }
        """
    )

    pkg_v1 = _build_pkg_from_lpmbuild(
        lpm,
        tmp_path,
        monkeypatch,
        name="hooks",
        version="1.0",
        release="1",
        install_body=install_body,
    )

    lpm.installpkg(pkg_v1, root=root, dry_run=False, verify=False, force=False, explicit=True)

    log_path = root / "hook.log"
    assert log_path.read_text().splitlines() == ["post_install 1.0-1 install"]

    pkg_v2 = _build_pkg_from_lpmbuild(
        lpm,
        tmp_path,
        monkeypatch,
        name="hooks",
        version="2.0",
        release="3",
        install_body=install_body,
    )

    lpm.installpkg(pkg_v2, root=root, dry_run=False, verify=False, force=False, explicit=True)

    assert log_path.read_text().splitlines() == [
        "post_install 1.0-1 install",
        "post_install 2.0-3 upgrade",
        "post_upgrade 2.0-3 1.0-1",
    ]
