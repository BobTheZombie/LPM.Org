"""JSON-backed fallback for the LPM state database.

The real project relies on :mod:`sqlite3`, but certain environments (notably
static ``libpython`` builds) ship without the necessary extension modules.  This
module implements the small subset of SQLite features that LPM uses so that the
program can still function with limited persistence.
"""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

__all__ = ["JsonBackedConnection", "connect"]


def _default_db() -> Dict[str, object]:
    return {
        "installed": {},
        "history": [],
        "snapshots": [],
        "_counters": {"history": 0, "snapshots": 0},
    }


class JsonCursor:
    def __init__(self, rows: Iterable[Tuple], lastrowid: Optional[int] = None):
        self._rows = list(rows)
        self._index = 0
        self.lastrowid = lastrowid

    def fetchone(self):
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchall(self):
        rows = self._rows[self._index :]
        self._index = len(self._rows)
        return rows

    def __iter__(self):
        return iter(self._rows)


class JsonBackedConnection:
    """Mimics the tiny slice of ``sqlite3.Connection`` that LPM uses."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._txn_backup: Optional[Dict[str, object]] = None
        self._dirty = False
        self._data: Dict[str, object] = _default_db()
        self._load()

    # ------------------------------------------------------------------ utils
    def _load(self) -> None:
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        self._data = data
            except Exception:
                self._data = _default_db()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        installed = self._data.setdefault("installed", {})
        history = self._data.setdefault("history", [])
        snapshots = self._data.setdefault("snapshots", [])
        counters = self._data.setdefault("_counters", {"history": 0, "snapshots": 0})
        counters.setdefault("history", 0)
        counters.setdefault("snapshots", 0)

        for row in installed.values():
            row.setdefault("symbols", "[]")
            row.setdefault("requires", "[]")
            row.setdefault("explicit", 0)
            row.setdefault("install_time", 0)

        self._data["installed"] = installed
        self._data["history"] = history
        self._data["snapshots"] = snapshots

    # ----------------------------------------------------------------- helpers
    def _installed_rows(self):
        installed: Dict[str, Dict[str, object]] = self._data["installed"]  # type: ignore[index]
        return installed

    def _history_rows(self) -> List[Dict[str, object]]:
        return self._data["history"]  # type: ignore[return-value]

    def _snapshot_rows(self) -> List[Dict[str, object]]:
        return self._data["snapshots"]  # type: ignore[return-value]

    def _next_id(self, key: str) -> int:
        counters: Dict[str, int] = self._data["_counters"]  # type: ignore[index]
        counters[key] = counters.get(key, 0) + 1
        self._dirty = True
        return counters[key]

    def _save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, sort_keys=True)
        self._dirty = False

    # ------------------------------------------------------------- API surface
    def executescript(self, script: str):  # pragma: no cover - not essential
        # Schema creation is implicit in JSON storage.
        return None

    def execute(self, sql: str, params: Tuple = ()):  # noqa: C901 - many branches
        norm = " ".join(sql.strip().split())
        with self._lock:
            if norm.upper() == "PRAGMA JOURNAL_MODE=WAL":
                return JsonCursor([("memory",)])
            if norm.upper() == "PRAGMA TABLE_INFO(INSTALLED)":
                cols = [
                    (0, "name", "TEXT", 1, None, 1),
                    (1, "version", "TEXT", 1, None, 0),
                    (2, "release", "TEXT", 1, None, 0),
                    (3, "arch", "TEXT", 1, None, 0),
                    (4, "provides", "TEXT", 1, None, 0),
                    (5, "symbols", "TEXT", 0, "[]", 0),
                    (6, "requires", "TEXT", 0, "[]", 0),
                    (7, "manifest", "TEXT", 1, None, 0),
                    (8, "explicit", "INTEGER", 0, 0, 0),
                    (9, "install_time", "INTEGER", 1, None, 0),
                ]
                return JsonCursor(cols)
            if norm.startswith("ALTER TABLE installed ADD COLUMN symbols"):
                for row in self._installed_rows().values():
                    if "symbols" not in row:
                        row["symbols"] = "[]"
                        self._dirty = True
                return JsonCursor([])
            if norm.startswith("ALTER TABLE installed ADD COLUMN requires"):
                for row in self._installed_rows().values():
                    if "requires" not in row:
                        row["requires"] = "[]"
                        self._dirty = True
                return JsonCursor([])
            if norm.startswith("ALTER TABLE installed ADD COLUMN explicit"):
                for row in self._installed_rows().values():
                    if "explicit" not in row:
                        row["explicit"] = 0
                        self._dirty = True
                return JsonCursor([])
            if norm.upper() == "BEGIN":
                if self._txn_backup is None:
                    self._txn_backup = deepcopy(self._data)
                return JsonCursor([])
            if norm.upper() == "COMMIT":
                self._txn_backup = None
                self._save()
                return JsonCursor([])
            if norm.upper() == "ROLLBACK":
                if self._txn_backup is not None:
                    self._data = deepcopy(self._txn_backup)
                    self._txn_backup = None
                    self._dirty = False
                return JsonCursor([])
            if norm.startswith("REPLACE INTO installed"):
                (
                    name,
                    version,
                    release,
                    arch,
                    provides,
                    symbols,
                    requires,
                    manifest,
                    explicit,
                    install_time,
                ) = params
                self._installed_rows()[name] = {
                    "name": name,
                    "version": version,
                    "release": release,
                    "arch": arch,
                    "provides": provides,
                    "symbols": symbols,
                    "requires": requires,
                    "manifest": manifest,
                    "explicit": int(explicit),
                    "install_time": int(install_time),
                }
                self._dirty = True
                return JsonCursor([])
            if norm.startswith("DELETE FROM installed WHERE name="):
                (name,) = params
                self._installed_rows().pop(name, None)
                self._dirty = True
                return JsonCursor([])
            if norm.startswith("INSERT INTO history"):
                (
                    ts,
                    action,
                    name,
                    from_ver,
                    to_ver,
                    details,
                ) = params
                entry_id = self._next_id("history")
                self._history_rows().append(
                    {
                        "id": entry_id,
                        "ts": int(ts),
                        "action": action,
                        "name": name,
                        "from_ver": from_ver,
                        "to_ver": to_ver,
                        "details": details,
                    }
                )
                return JsonCursor([], lastrowid=entry_id)
            if norm.startswith("DELETE FROM snapshots WHERE id="):
                (sid,) = params
                snapshots = self._snapshot_rows()
                new_list = [row for row in snapshots if row.get("id") != int(sid)]
                if len(new_list) != len(snapshots):
                    self._data["snapshots"] = new_list
                    self._dirty = True
                return JsonCursor([])
            if norm.startswith("INSERT INTO snapshots"):
                ts, tag, archive = params
                sid = self._next_id("snapshots")
                self._snapshot_rows().append(
                    {
                        "id": sid,
                        "ts": int(ts),
                        "tag": tag,
                        "archive": archive,
                    }
                )
                return JsonCursor([], lastrowid=sid)
            if norm.startswith("SELECT id,archive FROM snapshots ORDER BY id DESC"):
                rows = [
                    (row["id"], row["archive"])
                    for row in sorted(self._snapshot_rows(), key=lambda r: r["id"], reverse=True)
                ]
                return JsonCursor(rows)
            if norm.startswith("SELECT id,ts,tag,archive FROM snapshots ORDER BY id DESC"):
                rows = [
                    (row["id"], row["ts"], row["tag"], row["archive"])
                    for row in sorted(self._snapshot_rows(), key=lambda r: r["id"], reverse=True)
                ]
                return JsonCursor(rows)
            if norm.startswith("SELECT id,tag,archive FROM snapshots ORDER BY id DESC LIMIT 1"):
                rows = sorted(self._snapshot_rows(), key=lambda r: r["id"], reverse=True)
                if rows:
                    row = rows[0]
                    return JsonCursor([(row["id"], row["tag"], row["archive"])])
                return JsonCursor([])
            if norm.startswith("SELECT id,tag,archive FROM snapshots WHERE id="):
                (sid,) = params
                for row in self._snapshot_rows():
                    if row["id"] == int(sid):
                        return JsonCursor([(row["id"], row["tag"], row["archive"])])
                return JsonCursor([])
            if norm.startswith("SELECT archive FROM snapshots WHERE id="):
                (sid,) = params
                for row in self._snapshot_rows():
                    if row["id"] == int(sid):
                        return JsonCursor([(row["archive"],)])
                return JsonCursor([])
            if norm.startswith("SELECT id FROM snapshots WHERE archive="):
                (archive,) = params
                for row in self._snapshot_rows():
                    if row["archive"] == archive:
                        return JsonCursor([(row["id"],)])
                return JsonCursor([])
            if norm.startswith("SELECT ts,action,name,from_ver,to_ver FROM history ORDER BY id DESC LIMIT 200"):
                rows = sorted(self._history_rows(), key=lambda r: r["id"], reverse=True)[:200]
                return JsonCursor(
                    [(r["ts"], r["action"], r["name"], r["from_ver"], r["to_ver"]) for r in rows]
                )
            if norm.startswith("SELECT name,manifest FROM installed"):
                rows = [
                    (row["name"], row["manifest"])
                    for row in sorted(self._installed_rows().values(), key=lambda r: r["name"])
                ]
                return JsonCursor(rows)
            if norm.startswith("SELECT name,version,release,arch,provides,symbols,requires,manifest,explicit FROM installed"):
                rows = [
                    (
                        row["name"],
                        row["version"],
                        row["release"],
                        row["arch"],
                        row["provides"],
                        row["symbols"],
                        row["requires"],
                        row["manifest"],
                        row["explicit"],
                    )
                    for row in sorted(self._installed_rows().values(), key=lambda r: r["name"])
                ]
                return JsonCursor(rows)
            if norm.startswith("SELECT name,version,release,arch,install_time,explicit FROM installed ORDER BY name"):
                rows = [
                    (
                        row["name"],
                        row["version"],
                        row["release"],
                        row["arch"],
                        row["install_time"],
                        row["explicit"],
                    )
                    for row in sorted(self._installed_rows().values(), key=lambda r: r["name"])
                ]
                return JsonCursor(rows)
            if norm.startswith("SELECT manifest FROM installed WHERE name="):
                (name,) = params
                row = self._installed_rows().get(name)
                if row:
                    return JsonCursor([(row["manifest"],)])
                return JsonCursor([])
            if norm.startswith("SELECT version, release, manifest FROM installed WHERE name="):
                (name,) = params
                row = self._installed_rows().get(name)
                if row:
                    return JsonCursor([(row["version"], row["release"], row["manifest"])])
                return JsonCursor([])
            if norm.startswith("SELECT version, release FROM installed WHERE name="):
                (name,) = params
                row = self._installed_rows().get(name)
                if row:
                    return JsonCursor([(row["version"], row["release"])])
                return JsonCursor([])

        raise NotImplementedError(f"Unsupported SQL in fallback DB: {sql!r}")

    def commit(self) -> None:
        with self._lock:
            self._save()

    def close(self) -> None:
        with self._lock:
            if self._txn_backup is not None:
                self._data = deepcopy(self._txn_backup)
                self._txn_backup = None
                self._dirty = False
            else:
                self._save()


def connect(path: Path) -> JsonBackedConnection:
    return JsonBackedConnection(path)
