import os
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


def test_post_install_hooks_run(tmp_path, monkeypatch):
    root = tmp_path / "root"
    (root / "usr/share/icons/hicolor").mkdir(parents=True)
    (root / "usr/share/icons/hicolor/index.theme").write_text("[Icon Theme]")
    (root / "usr/share/applications").mkdir(parents=True)
    (root / "usr/share/applications/foo.desktop").write_text("[Desktop Entry]")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "log"
    for name in ("update-desktop-database", "gtk-update-icon-cache", "ldconfig"):
        p = bin_dir / name
        p.write_text(f"#!/bin/sh\necho {name} >> {log}\n")
        p.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    hook_dir = Path(__file__).resolve().parent.parent / "usr/share/lpm/hooks"
    monkeypatch.setattr(lpm, "HOOK_DIR", hook_dir)

    lpm.run_hook("post_install", {"LPM_ROOT": str(root)})

    calls = log.read_text().splitlines()
    assert "update-desktop-database" in calls
    assert "gtk-update-icon-cache" in calls
    assert "ldconfig" not in calls


def test_ldconfig_only_for_real_root(tmp_path, monkeypatch):
    hook_dir = Path(__file__).resolve().parent.parent / "usr/share/lpm/hooks"
    monkeypatch.setattr(lpm, "HOOK_DIR", hook_dir)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "log"
    for name in ("ldconfig", "update-desktop-database", "gtk-update-icon-cache"):
        p = bin_dir / name
        p.write_text(f"#!/bin/sh\necho {name} >> {log}\n")
        p.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    lpm.run_hook("post_install", {"LPM_ROOT": "/"})
    assert "ldconfig" in log.read_text().splitlines()

    log.write_text("")
    lpm.run_hook("post_install", {"LPM_ROOT": str(tmp_path)})
    assert "ldconfig" not in log.read_text().splitlines()


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
