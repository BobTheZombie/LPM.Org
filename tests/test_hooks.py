import json
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

for name in ("zstandard", "tqdm"):
    if name not in sys.modules:
        module = types.ModuleType(name)
        if name == "zstandard":
            class _DummyCompressor:
                def stream_writer(self, fh):
                    return fh

            class _DummyDecompressor:
                def stream_reader(self, fh):
                    return fh

            module.ZstdCompressor = _DummyCompressor
            module.ZstdDecompressor = _DummyDecompressor
        else:
            class _DummyTqdm:
                def __init__(self, iterable=None, **kwargs):
                    self.iterable = iterable

                def __iter__(self):
                    return iter(self.iterable or [])

                def update(self, *args, **kwargs):
                    return None

                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc, tb):
                    return False

            module.tqdm = _DummyTqdm  # type: ignore[attr-defined]

        sys.modules[name] = module

import lpm
import pytest
from src.liblpmhooks import HookTransactionManager, load_hooks


def test_python_hook(tmp_path, monkeypatch):
    hook_dir = tmp_path / "hooks"
    hook_dir.mkdir()
    monkeypatch.setattr(lpm, "HOOK_DIR", hook_dir)

    d = hook_dir / "sample.d"
    d.mkdir()
    marker = hook_dir / "ran"
    script = d / "hook.py"
    script.write_text(f"open({repr(str(marker))}, 'w').write('ok')")

    lpm.run_hook("sample", {})

    assert marker.read_text() == "ok"


def test_python_hook_falls_back_when_sys_executable_is_not_python(tmp_path, monkeypatch):
    hook_dir = tmp_path / "hooks"
    hook_dir.mkdir()
    monkeypatch.setattr(lpm, "HOOK_DIR", hook_dir)

    d = hook_dir / "sample.d"
    d.mkdir()
    marker = hook_dir / "ran"
    script = d / "hook.py"
    script.write_text(f"open({repr(str(marker))}, 'w').write('fallback')")

    monkeypatch.setattr(lpm.sys, "executable", str(tmp_path / "lpm"), raising=False)
    monkeypatch.setattr(lpm.sys, "frozen", True, raising=False)

    lpm.run_hook("sample", {})

    assert marker.read_text() == "fallback"


@pytest.fixture
def system_hook_dir(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parent.parent / "usr/share/liblpm/hooks"
    destination = tmp_path / "hooks"
    shutil.copytree(source, destination)
    exec_prefix = Path(__file__).resolve().parent.parent / "usr/libexec/lpm/hooks"
    for hook_file in destination.glob("*.hook"):
        text = hook_file.read_text()
        hook_file.write_text(text.replace("/usr/libexec/lpm/hooks/", f"{exec_prefix}/"))
    return destination


def test_system_hooks_run_via_transaction_manager(tmp_path, monkeypatch, system_hook_dir):
    root = tmp_path / "root"
    (root / "usr/share/icons/hicolor").mkdir(parents=True)
    (root / "usr/share/icons/hicolor/index.theme").write_text("[Icon Theme]")
    (root / "usr/share/applications").mkdir(parents=True)
    (root / "usr/share/applications/foo.desktop").write_text("[Desktop Entry]")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"
    for name in (
        "update-desktop-database",
        "gtk-update-icon-cache",
        "ldconfig",
        "systemd-sysusers",
        "systemd-tmpfiles",
        "udevadm",
    ):
        p = bin_dir / name
        p.write_text(f"#!/bin/sh\necho {name} \"$@\" >> {log}\n")
        p.chmod(0o755)
    systemd_hwdb = bin_dir / "systemd-hwdb"
    systemd_hwdb.write_text(f"#!/bin/sh\necho systemd-hwdb \"$@\" >> {log}\n")
    systemd_hwdb.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    hooks = load_hooks([system_hook_dir])
    txn = HookTransactionManager(hooks=hooks, root=root)
    txn.add_package_event(
        name="foo",
        operation="Install",
        version="1.0",
        release="1",
        paths=[
            "/usr/share/applications/foo.desktop",
            "/usr/share/icons/hicolor/index.theme",
            "/usr/lib/libfoo.so",
            "/etc/sysusers.d/foo.conf",
            "/usr/lib/tmpfiles.d/foo.conf",
            "/usr/lib/udev/hwdb.d/20-foo.hwdb",
        ],
    )
    txn.ensure_pre_transaction()
    txn.run_post_transaction()

    calls = log.read_text().splitlines()
    assert any(line.startswith("update-desktop-database") for line in calls)
    assert any("usr/share/applications" in line for line in calls)
    assert any(line.startswith("gtk-update-icon-cache") for line in calls)
    assert any("usr/share/icons/hicolor" in line for line in calls)
    assert any(
        line == f"systemd-sysusers --root {root}" for line in calls
    )
    assert any(
        line
        == "systemd-tmpfiles --create --remove --boot --root " f"{root}"
        for line in calls
    )
    assert sum(
        line.startswith("systemd-hwdb") or line.startswith("udevadm")
        for line in calls
    ) == 1
    assert any(
        line == f"systemd-hwdb update --root {root}"
        for line in calls
    )
    assert all(not line.startswith("ldconfig") for line in calls)


def test_ldconfig_runs_only_for_real_root(tmp_path, monkeypatch, system_hook_dir):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "ldconfig.log"
    p = bin_dir / "ldconfig"
    p.write_text(f"#!/bin/sh\necho ldconfig >> {log}\n")
    p.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    hooks = load_hooks([system_hook_dir])
    txn = HookTransactionManager(hooks=hooks, root=Path("/"))
    txn.add_package_event(
        name="glibc",
        operation="Upgrade",
        version="2.0",
        release="1",
        paths=["/usr/lib/libc.so"],
    )
    txn.ensure_pre_transaction()
    txn.run_post_transaction()
    assert "ldconfig" in log.read_text().splitlines()

    log.write_text("")
    txn = HookTransactionManager(hooks=hooks, root=tmp_path / "root")
    txn.add_package_event(
        name="glibc",
        operation="Upgrade",
        version="2.0",
        release="1",
        paths=["/usr/lib/libc.so"],
    )
    txn.ensure_pre_transaction()
    txn.run_post_transaction()
    assert log.read_text().strip() == ""


def test_systemd_daemon_reload_runs_only_for_real_root(
    tmp_path, monkeypatch, system_hook_dir
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "systemctl.log"
    log.write_text("")
    p = bin_dir / "systemctl"
    p.write_text(f"#!/bin/sh\necho systemctl \"$@\" >> {log}\n")
    p.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    hooks = load_hooks([system_hook_dir])
    txn = HookTransactionManager(hooks=hooks, root=Path("/"))
    txn.add_package_event(
        name="systemd-unit",
        operation="Install",
        version="1.0",
        release="1",
        paths=["/usr/lib/systemd/system/example.service"],
    )
    txn.ensure_pre_transaction()
    txn.run_post_transaction()
    assert "systemctl daemon-reload" in log.read_text().splitlines()

    log.write_text("")
    txn = HookTransactionManager(hooks=hooks, root=tmp_path / "root")
    txn.add_package_event(
        name="systemd-unit",
        operation="Install",
        version="1.0",
        release="1",
        paths=["/usr/lib/systemd/system/example.service"],
    )
    txn.ensure_pre_transaction()
    txn.run_post_transaction()
    assert log.read_text().strip() == ""


def test_kernel_install_hook(tmp_path, monkeypatch):
    hook_dir = Path(__file__).resolve().parent.parent / "usr/share/lpm/hooks"
    monkeypatch.setattr(lpm, "HOOK_DIR", hook_dir)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "log"
    for name in ("mkinitcpio", "bootctl", "grub-mkconfig"):
        p = bin_dir / name
        p.write_text(f"#!/bin/sh\necho {name} \"$@\" >> {log}\n")
        p.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    lpm.run_hook("kernel_install", {"LPM_PRESET": "test"})

    calls = log.read_text().splitlines()
    assert "mkinitcpio -p test" in calls
    assert "bootctl update" in calls
    assert "grub-mkconfig -o /boot/grub/grub.cfg" in calls


def _create_hook_recorder(tmp_path: Path, script_name: str) -> Path:
    script_path = tmp_path / script_name
    script_path.write_text(
        "#!" + sys.executable + "\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "log = Path(sys.argv[1])\n"
        "payload = {\"hook\": os.environ.get(\"LPM_HOOK_NAME\"), \"args\": sys.argv[2:], \"targets\": os.environ.get(\"LPM_TARGETS\", \"\") }\n"
        "with log.open('a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(payload) + \"\\n\")\n"
    )
    script_path.chmod(0o755)
    return script_path


def test_transaction_manager_package_hooks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    system = tmp_path / "system"
    admin = tmp_path / "admin"
    system.mkdir()
    admin.mkdir()
    scripts = tmp_path / "scripts"
    scripts.mkdir()

    log_path = tmp_path / "log.jsonl"
    recorder = _create_hook_recorder(scripts, "record.py")

    (system / "alpha.hook").write_text(
        "[Trigger]\n"
        "Type = Package\n"
        "Operation = Install\n"
        "Target = foo\n"
        "\n"
        "[Action]\n"
        "When = PreTransaction\n"
        f"Exec = {recorder} {log_path}\n"
        "NeedsTargets = true\n"
    )

    (system / "beta.hook").write_text(
        "[Trigger]\n"
        "Type = Package\n"
        "Operation = Install\n"
        "Target = foo\n"
        "\n"
        "[Action]\n"
        "When = PreTransaction\n"
        f"Exec = {recorder} {log_path}\n"
        "NeedsTargets = true\n"
        "Depends = alpha\n"
    )

    hooks = load_hooks([system, admin])
    txn = HookTransactionManager(hooks=hooks, root=root)
    txn.add_package_event(name="foo", operation="Install", version="1.0", release="1", paths=["/usr/bin/foo"])
    txn.ensure_pre_transaction()

    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert [entry["hook"] for entry in entries] == ["alpha", "beta"]
    assert entries[0]["args"] == ["foo-1.0-1"]
    assert entries[0]["targets"] == "foo-1.0-1"


def test_transaction_manager_path_hooks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()
    scripts = tmp_path / "scripts"
    scripts.mkdir()

    log_path = tmp_path / "paths.jsonl"
    recorder = _create_hook_recorder(scripts, "record_paths.py")

    (hooks_dir / "pathcheck.hook").write_text(
        "[Trigger]\n"
        "Type = Path\n"
        "Operation = Install\n"
        "Target = usr/bin/foo\n"
        "\n"
        "[Action]\n"
        "When = PostTransaction\n"
        f"Exec = {recorder} {log_path}\n"
        "NeedsTargets = true\n"
    )

    hooks = load_hooks([hooks_dir])
    txn = HookTransactionManager(hooks=hooks, root=root)
    txn.add_package_event(
        name="foo",
        operation="Install",
        version="1.0",
        release="1",
        paths=["/usr/bin/foo", "/usr/share/doc/readme"],
    )
    txn.ensure_pre_transaction()
    txn.run_post_transaction()

    entries = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert entries[0]["hook"] == "pathcheck"
    assert entries[0]["args"] == ["/usr/bin/foo"]
    assert entries[0]["targets"] == "/usr/bin/foo"


def test_transaction_manager_failure_handling(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    hooks_dir = tmp_path / "hooks"
    hooks_dir.mkdir()

    fail_script = tmp_path / "fail.sh"
    fail_script.write_text("#!/bin/sh\nexit 1\n")
    fail_script.chmod(0o755)

    (hooks_dir / "fatal.hook").write_text(
        "[Trigger]\n"
        "Type = Package\n"
        "Operation = Install\n"
        "Target = failpkg\n"
        "\n"
        "[Action]\n"
        "When = PreTransaction\n"
        f"Exec = {fail_script}\n"
        "AbortOnFail = true\n"
    )

    hooks = load_hooks([hooks_dir])
    txn = HookTransactionManager(hooks=hooks, root=root)
    txn.add_package_event(name="failpkg", operation="Install", version="1", release="1", paths=["/tmp/file"])

    with pytest.raises(subprocess.CalledProcessError):
        txn.ensure_pre_transaction()

    (hooks_dir / "fatal.hook").unlink()
    (hooks_dir / "nonfatal.hook").write_text(
        "[Trigger]\n"
        "Type = Package\n"
        "Operation = Install\n"
        "Target = failpkg\n"
        "\n"
        "[Action]\n"
        "When = PreTransaction\n"
        f"Exec = {fail_script}\n"
        "AbortOnFail = false\n"
    )

    hooks = load_hooks([hooks_dir])
    txn = HookTransactionManager(hooks=hooks, root=root)
    txn.add_package_event(name="failpkg", operation="Install", version="1", release="1", paths=["/tmp/file"])
    txn.ensure_pre_transaction()
