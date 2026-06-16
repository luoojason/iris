"""The fold-back inbox: per-conversation notes for the agent's next turn.

Background work (a finished job, a fired wake) must reach the *conversation*,
not just the owner's Discord ping, without any model call of its own. Producers
append plain-text entries TAGGED with the conversation they belong to, and
``Agent.respond`` drains only the current conversation's entries and folds them
into that turn's prompt. That scoping is load-bearing: it keeps a job started in
one Discord thread from surfacing its report in an unrelated thread (every turn
used to drain one global queue, so a finished job folded into whatever
conversation messaged next).

A note carries the conversation id it belongs to; ``drain(conversation_id)``
returns only matching notes. Notes with no id live in the ``None`` bucket and are
only taken by ``drain(None)``, so an untagged/legacy note never bleeds into a
real conversation. If a turn errors, drained entries are restored, so a flaky
turn cannot eat a report.

File-backed with flock + atomic-replace. Capped: past INBOX_CAP entries the
oldest are dropped, so a runaway producer cannot grow the next prompt without
bound.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

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

    def _load(self) -> list[dict]:
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
        items: list[dict] = []
        for item in data:
            if isinstance(item, str):  # legacy untagged note
                items.append({"conversation_id": None, "text": item})
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                items.append({"conversation_id": item.get("conversation_id"),
                              "text": item["text"]})
        return items

    def _save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(items, handle, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def append(self, text: str, conversation_id: Optional[str] = None) -> None:
        with self._locked():
            items = self._load()
            items.append({"conversation_id": conversation_id, "text": text})
            if len(items) > INBOX_CAP:
                items = items[-INBOX_CAP:]
            self._save(items)

    def drain(self, conversation_id: Optional[str] = None) -> list[str]:
        """Atomically take this conversation's queued entries, leaving the rest."""
        with self._locked():
            items = self._load()
            taken = [it["text"] for it in items if it.get("conversation_id") == conversation_id]
            if taken:
                kept = [it for it in items if it.get("conversation_id") != conversation_id]
                self._save(kept)
            return taken

    def restore(self, texts: list[str], conversation_id: Optional[str] = None) -> None:
        """Put drained entries back at the front (the turn that took them failed)."""
        if not texts:
            return
        with self._locked():
            current = self._load()
            merged = [{"conversation_id": conversation_id, "text": t} for t in texts] + current
            if len(merged) > INBOX_CAP:
                # Keep the front: the just-restored entries lead the list, so an
                # over-cap trim must drop the oldest tail, not the fresh restore.
                merged = merged[:INBOX_CAP]
            self._save(merged)
