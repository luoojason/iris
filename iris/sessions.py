"""Map each conversation to a persistent ``claude`` session id.

A "conversation" is whatever the chat platform considers one thread: a Discord
channel, a DM, a Telegram chat. Holding one ``claude`` session id per
conversation is what gives the agent memory across turns, since the next turn
runs ``claude -p --resume <session_id>``.

The store is a small JSON file plus an in-process lock. It is deliberately
boring: no database, no daemon, easy to inspect and back up.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional


class SessionStore:
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
                # A corrupt store should not take the agent down. Start fresh
                # but keep the bad file around for inspection.
                try:
                    self.path.replace(self.path.with_suffix(self.path.suffix + ".corrupt"))
                except OSError:
                    pass
                self._data = {}

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def get(self, conversation_id: str) -> Optional[str]:
        with self._lock:
            entry = self._data.get(conversation_id)
            return entry.get("session_id") if entry else None

    def set(self, conversation_id: str, session_id: str) -> None:
        with self._lock:
            self._data[conversation_id] = {
                "session_id": session_id,
                "updated_at": time.time(),
            }
            self._flush()

    def clear(self, conversation_id: str) -> bool:
        """Forget a conversation so the next message starts a fresh session."""
        with self._lock:
            existed = conversation_id in self._data
            self._data.pop(conversation_id, None)
            if existed:
                self._flush()
            return existed

    def all(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._data)
