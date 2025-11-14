from __future__ import annotations

import errno
import logging
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, MutableMapping, Optional, Sequence, Set

logger = logging.getLogger(__name__)

module = sys.modules[__name__]

# Expose the module under legacy import paths so monkeypatch helpers and code
# depending on ``src.liblpmhooks`` continue to function after the transition to
# the :mod:`lpm` package.
sys.modules.setdefault(__name__ + ".__init__", module)
sys.modules.setdefault("src.liblpmhooks", module)
sys.modules.setdefault("src.liblpmhooks.__init__", module)
if not isinstance(getattr(module, "__init__", None), type(module)):
    setattr(module, "__init__", module)

__all__ = [
    "Hook",
    "HookTrigger",
    "HookAction",
    "HookTransactionManager",
    "HookError",
    "load_hooks",
]


@dataclass
class HookTrigger:
    type: str
    operations: Set[str] = field(default_factory=set)
    targets: List[str] = field(default_factory=list)


@dataclass
class HookAction:
    when: str
    exec: List[str]
    needs_targets: bool = False
    depends: List[str] = field(default_factory=list)
    abort_on_fail: bool = False


@dataclass
class Hook:
    name: str
    path: Path
    triggers: List[HookTrigger]
    action: HookAction


_VALID_TYPES = {"Path", "Package"}
_VALID_OPS = {"Install", "Upgrade", "Remove"}
_VALID_WHEN = {"PreTransaction", "PostTransaction"}

_TYPE_MAP = {value.lower(): value for value in _VALID_TYPES}
_OP_MAP = {value.lower(): value for value in _VALID_OPS}
_WHEN_MAP = {value.lower(): value for value in _VALID_WHEN}


class HookError(RuntimeError):
    pass


def _iter_hook_files(paths: Sequence[Path]) -> Iterator[Path]:
    for base in paths:
        try:
            entries = sorted(Path(base).glob("*.hook"))
        except OSError:
            continue
        for entry in entries:
            if entry.is_file():
                yield entry


def _parse_hook(path: Path) -> Hook:
    current: Optional[str] = None
    triggers: List[HookTrigger] = []
    trigger: Optional[HookTrigger] = None
    action_data: MutableMapping[str, List[str]] = {}

    def finalize_trigger() -> None:
        nonlocal trigger
        if trigger is not None:
            if not trigger.type:
                raise HookError(f"{path}: Trigger missing Type")
            if not trigger.operations:
                raise HookError(f"{path}: Trigger missing Operation")
            if not trigger.targets:
                raise HookError(f"{path}: Trigger missing Target")
            triggers.append(trigger)
        trigger = None

    with path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                if section == "Trigger":
                    finalize_trigger()
                    trigger = HookTrigger(type="", operations=set(), targets=[])
                elif section == "Action":
                    finalize_trigger()
                    current = "Action"
                else:
                    raise HookError(f"{path}: Unknown section {section!r}")
                current = section
                continue
            if current == "Trigger":
                if trigger is None:
                    trigger = HookTrigger(type="", operations=set(), targets=[])
                if "=" in line:
                    key, value = [item.strip() for item in line.split("=", 1)]
                else:
                    key, value = line, "true"
                key = key.capitalize()
                if key == "Type":
                    normalized = value.strip().lower()
                    if normalized not in _TYPE_MAP:
                        raise HookError(f"{path}: Invalid Trigger Type {value!r}")
                    trigger.type = _TYPE_MAP[normalized]
                elif key == "Operation":
                    normalized = value.strip().lower()
                    if normalized not in _OP_MAP:
                        raise HookError(f"{path}: Invalid Operation {value!r}")
                    trigger.operations.add(_OP_MAP[normalized])
                elif key == "Target":
                    trigger.targets.append(value)
                else:
                    logger.debug("Ignoring unknown trigger key %s in %s", key, path)
            elif current == "Action":
                if "=" in line:
                    key, value = [item.strip() for item in line.split("=", 1)]
                else:
                    key, value = line, "true"
                action_data.setdefault(key, []).append(value)
            else:
                raise HookError(f"{path}: Entry outside of [Trigger]/[Action] sections")

    finalize_trigger()
    if not triggers:
        raise HookError(f"{path}: hook must define at least one [Trigger]")
    if "When" not in action_data:
        raise HookError(f"{path}: Action missing When")
    if "Exec" not in action_data:
        raise HookError(f"{path}: Action missing Exec")

    when_raw = action_data["When"][0]
    when_value = when_raw.strip().lower()
    if when_value not in _WHEN_MAP:
        raise HookError(f"{path}: Invalid When {when_raw!r}")
    when = _WHEN_MAP[when_value]

    exec_values = action_data["Exec"]
    if len(exec_values) != 1:
        raise HookError(f"{path}: Action Exec must appear exactly once")
    exec_tokens = shlex.split(exec_values[0])
    if not exec_tokens:
        raise HookError(f"{path}: Exec command is empty")

    needs_targets = False
    if "NeedsTargets" in action_data:
        needs_targets = any(_str_to_bool(val) for val in action_data["NeedsTargets"])
    depends: List[str] = []
    for dep_line in action_data.get("Depends", []):
        depends.extend(part for part in dep_line.split() if part)
    abort_on_fail = False
    if "AbortOnFail" in action_data:
        abort_on_fail = any(_str_to_bool(val) for val in action_data["AbortOnFail"])

    action = HookAction(
        when=when,
        exec=exec_tokens,
        needs_targets=needs_targets,
        depends=depends,
        abort_on_fail=abort_on_fail,
    )

    return Hook(name=path.stem, path=path, triggers=triggers, action=action)


def _str_to_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_hooks(paths: Sequence[Path]) -> Dict[str, Hook]:
    hooks: Dict[str, Hook] = {}
    for hook_path in _iter_hook_files(paths):
        try:
            hook = _parse_hook(hook_path)
        except HookError as exc:
            logger.warning("Ignoring invalid hook %s: %s", hook_path, exc)
            continue
        hooks[hook.name] = hook
    return hooks


@dataclass
class _TransactionEvent:
    name: str
    operation: str
    version: Optional[str]
    release: Optional[str]
    paths: List[str]

    def package_target(self) -> str:
        if self.version and self.release:
            return f"{self.name}-{self.version}-{self.release}"
        if self.version:
            return f"{self.name}-{self.version}"
        return self.name


class HookTransactionManager:
    def __init__(
        self,
        *,
        hooks: Mapping[str, Hook],
        root: Path,
        base_env: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.hooks = dict(hooks)
        self.root = Path(root)
        self.base_env = dict(base_env or {})
        self.events: List[_TransactionEvent] = []
        self._pre_ran = False
        self._post_ran = False

    def add_package_event(
        self,
        *,
        name: str,
        operation: str,
        version: Optional[str],
        release: Optional[str],
        paths: Iterable[str],
    ) -> None:
        op = operation.capitalize()
        if op not in _VALID_OPS:
            raise HookError(f"Unsupported operation {operation!r}")
        cleaned_paths = _dedupe_preserve_order(_normalize_path(p) for p in paths)
        self.events.append(
            _TransactionEvent(
                name=name,
                operation=op,
                version=version,
                release=release,
                paths=cleaned_paths,
            )
        )

    def ensure_pre_transaction(self) -> None:
        if not self._pre_ran:
            self._run_when("PreTransaction")
            self._pre_ran = True

    def run_post_transaction(self) -> None:
        if not self._post_ran:
            if not self._pre_ran:
                # If post is requested without pre, still mark pre as executed so
                # hooks relying solely on post run correctly.
                self._pre_ran = True
            self._run_when("PostTransaction")
            self._post_ran = True

    # ------------------------------------------------------------------
    def _gather_matches(self, trigger: HookTrigger) -> List[str]:
        matches: List[str] = []
        for event in self.events:
            if event.operation not in trigger.operations:
                continue
            if trigger.type == "Package":
                for target in trigger.targets:
                    if fnmatch(event.name, target):
                        pkg_target = event.package_target()
                        if pkg_target not in matches:
                            matches.append(pkg_target)
                        break
            elif trigger.type == "Path":
                for path in event.paths:
                    for target in trigger.targets:
                        if fnmatch(path, target) or fnmatch(path.lstrip("/"), target):
                            if path not in matches:
                                matches.append(path)
                            break
        return matches

    def _iter_triggered(self, when: str) -> Iterator[tuple[Hook, List[str]]]:
        for hook in self.hooks.values():
            if hook.action.when != when:
                continue
            targets: List[str] = []
            for trigger in hook.triggers:
                matches = self._gather_matches(trigger)
                if matches:
                    targets.extend(matches)
            if targets:
                yield hook, _dedupe_preserve_order(targets)

    def _run_when(self, when: str) -> None:
        triggered: List[tuple[Hook, List[str]]] = list(self._iter_triggered(when))
        if not triggered:
            return
        ordered = self._order_by_dependencies(triggered)
        for hook, targets in ordered:
            self._run_hook(hook, targets)

    def _order_by_dependencies(
        self, triggered: List[tuple[Hook, List[str]]]
    ) -> List[tuple[Hook, List[str]]]:
        hooks_by_name = {hook.name: (hook, targets) for hook, targets in triggered}
        deps: Dict[str, Set[str]] = {}
        for hook, _ in triggered:
            dep_names = [d for d in hook.action.depends if d in hooks_by_name]
            deps[hook.name] = set(dep_names)
        resolved: List[tuple[Hook, List[str]]] = []
        ready = [name for name, needed in deps.items() if not needed]
        while ready:
            name = ready.pop(0)
            hook, targets = hooks_by_name[name]
            resolved.append((hook, targets))
            for other, needed in deps.items():
                if name in needed:
                    needed.remove(name)
                    if not needed and other not in [h.name for h, _ in resolved] and other in hooks_by_name:
                        ready.append(other)
        if len(resolved) != len(hooks_by_name):
            missing = {name for name in hooks_by_name if name not in [h.name for h, _ in resolved]}
            raise HookError(f"Cyclic or unresolved hook dependencies: {', '.join(sorted(missing))}")
        return resolved

    def _run_hook(self, hook: Hook, targets: List[str]) -> None:
        action = hook.action
        base_argv = list(action.exec)
        env = os.environ.copy()
        env.update(self.base_env)
        env.update(
            {
                "LPM_HOOK_NAME": hook.name,
                "LPM_HOOK_PATH": str(hook.path),
                "LPM_HOOK_WHEN": action.when,
                "LPM_ROOT": str(self.root),
            }
        )
        if action.needs_targets:
            env["LPM_TARGET_COUNT"] = str(len(targets))
            env["LPM_TARGETS"] = "\n".join(targets)
            argv = base_argv + targets
            if _should_use_temp_targets(argv, env):
                logger.info(
                    "Hook %s command line would exceed safe argument limits; "
                    "using temporary targets file",
                    hook.name,
                )
                _run_hook_with_temp_targets(
                    base_argv=base_argv,
                    env=env,
                    hook_name=hook.name,
                    targets=targets,
                )
                return
        else:
            argv = base_argv
        try:
            subprocess.run(argv, check=True, env=env)
        except OSError as exc:
            if exc.errno == errno.E2BIG and action.needs_targets:
                logger.warning(
                    "Hook %s command line exceeded argument limits; "
                    "retrying without target arguments",
                    hook.name,
                )
                _run_hook_with_temp_targets(
                    base_argv=base_argv,
                    env=env,
                    hook_name=hook.name,
                    targets=targets,
                )
                return
            raise
        except subprocess.CalledProcessError as exc:
            logger.error("Hook %s failed: %s", hook.name, exc)
            if action.abort_on_fail:
                raise


def _should_use_temp_targets(
    argv: Sequence[str], env: Mapping[str, str]
) -> bool:
    arg_max = _get_arg_max()
    if arg_max <= 0:
        return False
    size = _estimate_command_size(argv, env)
    threshold = max(arg_max - 4096, int(arg_max * 0.8))
    return size >= threshold


def _estimate_command_size(argv: Sequence[str], env: Mapping[str, str]) -> int:
    size = 0
    for value in argv:
        size += len(value) + 1
    for key, value in env.items():
        size += len(key) + len(value) + 2
    return size


def _get_arg_max() -> int:
    try:
        arg_max = os.sysconf("SC_ARG_MAX")
    except (ValueError, OSError, AttributeError):
        return 0
    if isinstance(arg_max, int) and arg_max > 0:
        return arg_max
    return 0


def _run_hook_with_temp_targets(
    *, base_argv: Sequence[str], env: Mapping[str, str], hook_name: str, targets: Sequence[str]
) -> None:
    """Execute *base_argv* with target data stored in a temporary file."""

    temp_path: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as fh:
            temp_path = fh.name
            for target in targets:
                fh.write(f"{target}\n")
        fallback_env = dict(env)
        fallback_env.pop("LPM_TARGETS", None)
        fallback_env["LPM_TARGETS_FILE"] = temp_path
        fallback_env["LPM_TARGET_COUNT"] = str(len(targets))
        subprocess.run(base_argv, check=True, env=fallback_env)
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                logger.warning(
                    "Unable to clean up temporary targets file for hook %s", hook_name,
                    exc_info=True,
                )


def _normalize_path(path: str) -> str:
    if not path:
        return path
    text = path.replace("\\", "/")
    if not text.startswith("/"):
        text = "/" + text
    while "//" in text:
        text = text.replace("//", "/")
    return text


def _dedupe_preserve_order(items: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    result: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
