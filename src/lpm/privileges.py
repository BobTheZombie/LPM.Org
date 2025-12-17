from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional, Tuple


def _env_int(name: str) -> Optional[int]:
    value = os.environ.get(name)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class _PrivilegeInfo:
    privileged_uid: int
    privileged_gid: int
    unpriv_uid: int
    unpriv_gid: int


class _PrivilegeManager:
    """Co-ordinate temporary privilege escalation for sensitive operations."""

    def __init__(self) -> None:
        privileged_uid = os.geteuid()
        privileged_gid = os.getegid()

        sudo_uid = _env_int("SUDO_UID")
        sudo_gid = _env_int("SUDO_GID")
        pkexec_uid = _env_int("PKEXEC_UID")
        pkexec_gid = _env_int("PKEXEC_GID")

        real_uid = os.getuid()
        real_gid = os.getgid()

        unpriv_uid = (
            sudo_uid
            if sudo_uid is not None
            else pkexec_uid
            if pkexec_uid is not None
            else real_uid
        )
        unpriv_gid = (
            sudo_gid
            if sudo_gid is not None
            else pkexec_gid
            if pkexec_gid is not None
            else real_gid
        )

        self.info = _PrivilegeInfo(
            privileged_uid=privileged_uid,
            privileged_gid=privileged_gid,
            unpriv_uid=unpriv_uid,
            unpriv_gid=unpriv_gid,
        )

        self.enabled = (
            privileged_uid == 0
            and unpriv_uid != privileged_uid
        )

        self._lock = threading.RLock()
        self._depth = 0

        if self.enabled:
            self._drop_privileges()

    def _apply_gid(self, effective_gid: int) -> None:
        info = self.info
        if hasattr(os, "setresgid"):
            os.setresgid(info.unpriv_gid, effective_gid, info.privileged_gid)
        elif hasattr(os, "setregid"):
            os.setregid(info.unpriv_gid, effective_gid)
        else:
            os.setegid(effective_gid)

    def _apply_uid(self, effective_uid: int) -> None:
        info = self.info
        if hasattr(os, "setresuid"):
            os.setresuid(info.unpriv_uid, effective_uid, info.privileged_uid)
        elif hasattr(os, "setreuid"):
            os.setreuid(info.unpriv_uid, effective_uid)
        else:
            os.seteuid(effective_uid)

    def _drop_privileges(self) -> None:
        if not self.enabled:
            return
        # Drop group privileges before the user ID while we still have rights.
        self._apply_gid(self.info.unpriv_gid)
        self._apply_uid(self.info.unpriv_uid)

    def _raise_privileges(self) -> None:
        if not self.enabled:
            return
        # Regain root (effective) privileges first so group changes succeed.
        self._apply_uid(self.info.privileged_uid)
        self._apply_gid(self.info.privileged_gid)

    def acquire(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            if self._depth == 0:
                self._raise_privileges()
            self._depth += 1
        return True

    def release(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            if self._depth == 0:
                return
            self._depth -= 1
            if self._depth == 0:
                self._drop_privileges()


_MANAGER = _PrivilegeManager()


@contextmanager
def privileged_section() -> Iterator[None]:
    """Temporarily escalate to privileged credentials when available."""

    token = _MANAGER.acquire()
    try:
        yield
    finally:
        if token:
            _MANAGER.release()


def privileges_enabled() -> bool:
    return _MANAGER.enabled


def privilege_info() -> _PrivilegeInfo:
    return _MANAGER.info


def state_owner_ids() -> tuple[Optional[int], Optional[int]]:
    info = privilege_info()
    if privileges_enabled() and info.privileged_uid == 0 and info.unpriv_uid != info.privileged_uid:
        return info.unpriv_uid, info.unpriv_gid
    return None, None


__all__ = [
    "privileged_section",
    "privileges_enabled",
    "privilege_info",
    "state_owner_ids",
]
