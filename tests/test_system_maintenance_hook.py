from importlib.machinery import SourceFileLoader
import types
from pathlib import Path


def _load_module():
    repo_root = Path(__file__).resolve().parent.parent
    hook_path = repo_root / "usr/libexec/lpm/hooks/system-maintenance"
    loader = SourceFileLoader("system_maintenance", str(hook_path))
    module = types.ModuleType(loader.name)
    loader.exec_module(module)
    return module


def test_skip_autoremove_on_remove_transaction():
    mod = _load_module()
    assert mod._should_skip_autoremove({"remove"}) is True


def test_skip_autoremove_on_install_only_transaction():
    mod = _load_module()
    assert mod._should_skip_autoremove({"install"}) is True


def test_run_autoremove_for_upgrade_transactions():
    mod = _load_module()
    assert mod._should_skip_autoremove({"upgrade"}) is False


def test_run_autoremove_when_no_recent_actions():
    mod = _load_module()
    assert mod._should_skip_autoremove(set()) is False
