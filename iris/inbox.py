"""The fold-back inbox: notes for the agent's next owner-initiated turn.

Background work (a finished job, a fired wake) must reach the *conversation*,
not just the owner's Discord ping, without any model call of its own. So
producers append plain-text entries here, and `Agent.respond` drains the
inbox on the next turn and folds the entries into the prompt. If that turn
errors, the entries are restored, so a flaky turn cannot eat a report.

File-backed with the same flock + atomic-replace pattern as the other
stores. Capped: past INBOX_CAP entries the oldest are dropped, so a runaway
producer cannot grow the next prompt without bound.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

INBOX_CAP = 50


class Inbox:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None:
            yield
            return
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        with open(lock, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _load(self) -> list[str]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            from .statefile import quarantine_corrupt
            quarantine_corrupt(self.path, "fold-back inbox")
            return []
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, str)]

    def _save(self, items: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(items, handle, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def append(self, text: str) -> None:
        with self._locked():
            items = self._load()
            items.append(text)
            if len(items) > INBOX_CAP:
                items = items[-INBOX_CAP:]
            self._save(items)

    def drain(self) -> list[str]:
        """Atomically take every queued entry."""
        with self._locked():
            items = self._load()
            if items:
                self._save([])
            return items

    def restore(self, items: list[str]) -> None:
        """Put drained entries back at the front (the turn that took them failed)."""
        if not items:
            return
        with self._locked():
            current = self._load()
            merged = list(items) + current
            if len(merged) > INBOX_CAP:
                merged = merged[-INBOX_CAP:]
            self._save(merged)
