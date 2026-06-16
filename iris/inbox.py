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

import os
from typing import Optional

from .statefile import JsonListStore

INBOX_CAP = 50


class Inbox:
    def __init__(self, path: str | os.PathLike[str]):
        # The lock/atomic-write/corruption-recovery plumbing lives in the shared
        # JsonListStore; this class holds only the inbox's domain rules.
        self._store = JsonListStore(path, "fold-back inbox")
        self.path = self._store.path

    def _load(self) -> list[dict]:
        """Load and normalize: legacy untagged strings become tagged-None notes,
        and anything without a string ``text`` is dropped."""
        items: list[dict] = []
        for item in self._store.load():
            if isinstance(item, str):  # legacy untagged note
                items.append({"conversation_id": None, "text": item})
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                items.append({"conversation_id": item.get("conversation_id"),
                              "text": item["text"]})
        return items

    def append(self, text: str, conversation_id: Optional[str] = None) -> None:
        with self._store.locked():
            items = self._load()
            items.append({"conversation_id": conversation_id, "text": text})
            if len(items) > INBOX_CAP:
                items = items[-INBOX_CAP:]
            self._store.save(items)

    def drain(self, conversation_id: Optional[str] = None) -> list[str]:
        """Atomically take this conversation's queued entries, leaving the rest."""
        with self._store.locked():
            items = self._load()
            taken = [it["text"] for it in items if it.get("conversation_id") == conversation_id]
            if taken:
                kept = [it for it in items if it.get("conversation_id") != conversation_id]
                self._store.save(kept)
            return taken

    def restore(self, texts: list[str], conversation_id: Optional[str] = None) -> None:
        """Put drained entries back at the front (the turn that took them failed)."""
        if not texts:
            return
        with self._store.locked():
            current = self._load()
            merged = [{"conversation_id": conversation_id, "text": t} for t in texts] + current
            if len(merged) > INBOX_CAP:
                # Keep the front: the just-restored entries lead the list, so an
                # over-cap trim must drop the oldest tail, not the fresh restore.
                merged = merged[:INBOX_CAP]
            self._store.save(merged)
