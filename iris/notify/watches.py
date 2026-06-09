"""File-backed store of change-watches and their last-seen values.

Mirrors SessionStore / ReminderStore: a small JSON file plus an in-process lock,
written atomically (temp file, fsync, rename). Keyed by watch name.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional


def new_watch(name, *, url=None, cmd=None, extract_kind="text", extract_arg="", every_seconds=0.0):
    """Build a watch definition with empty last-seen state."""
    return {
        "name": name,
        "url": url,
        "cmd": cmd,
        "extract": {"kind": extract_kind, "arg": extract_arg},
        "every_seconds": float(every_seconds),
        "last_value": None,
        "last_checked": 0.0,
        "last_changed": 0.0,
    }


class WatchStore:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text("utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def add(self, watch: dict) -> None:
        with self._lock:
            self._data[watch["name"]] = watch
            self._flush()

    def remove(self, name: str) -> bool:
        with self._lock:
            existed = name in self._data
            self._data.pop(name, None)
            if existed:
                self._flush()
            return existed

    def get(self, name: str) -> Optional[dict]:
        with self._lock:
            return self._data.get(name)

    def list(self) -> list[dict]:
        with self._lock:
            return list(self._data.values())

    def record(self, name: str, value: str, checked_ts: float, changed: bool) -> None:
        with self._lock:
            watch = self._data.get(name)
            if not watch:
                return
            watch["last_value"] = value
            watch["last_checked"] = checked_ts
            if changed:
                watch["last_changed"] = checked_ts
            self._flush()

    def due(self, now: float) -> list[dict]:
        with self._lock:
            return [
                w for w in self._data.values()
                if w.get("last_checked", 0.0) + w.get("every_seconds", 0.0) <= now
            ]
