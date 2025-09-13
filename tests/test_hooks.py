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
