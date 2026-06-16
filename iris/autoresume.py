"""Owner-initiated chains may self-continue: the autonomous-resume queue.

Zero idle inference is relaxed here in exactly one bounded way. Normally a
finished background command only folds a note into the inbox and pings the home
channel, then waits for the owner's next message. When the owner has turned this
on (``IRIS_AUTO_RESUME``) and asked for it on a specific launch
(``run_in_background(autoresume=True)``), the finished command instead enqueues
a resume request, and the *bot process* drains the queue and runs one follow-up
turn on the home conversation — so a chain the owner started can carry itself to
the next step and report back.

It is never inference from nothing: a request exists only because the owner
launched the task. It is bounded — a per-UTC-day cap, suppressed under credit
park, off by default — and the resume runs through the same per-conversation
runner as a typed message, so it cannot race the live session.

The producer (``iris watch --resume``) and consumer (the bot poll loop) are
separate processes, so the queue is file-backed with the same flock +
atomic-replace pattern as the inbox. See
docs/superpowers/specs/2026-06-12-auto-resume-design.md.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

from .statefile import JsonDictStore, JsonListStore


class ResumeQueue:
    """A file-backed queue of pending autonomous-resume requests.

    Each entry is ``{"conversation_id", "prompt", "ts"}``. Capped like the
    inbox so a runaway producer cannot grow it without bound: past ``CAP`` the
    oldest entries are dropped.
    """

    CAP = 50

    def __init__(self, path: str | os.PathLike[str]):
        self._store = JsonListStore(path, "resume queue")
        self.path = self._store.path

    @contextmanager
    def _locked(self):
        with self._store.locked():
            yield

    def _load(self) -> list[dict]:
        return [item for item in self._store.load() if isinstance(item, dict)]

    def _save(self, items: list[dict]) -> None:
        self._store.save(items)

    def enqueue(self, conversation_id: str, prompt: str) -> None:
        with self._locked():
            items = self._load()
            items.append({
                "conversation_id": conversation_id,
                "prompt": prompt,
                "ts": time.time(),
            })
            if len(items) > self.CAP:
                items = items[-self.CAP:]
            self._save(items)

    def drain(self) -> list[dict]:
        """Atomically take every queued request (oldest first)."""
        with self._locked():
            items = self._load()
            if items:
                self._save([])
            return items


def _utc_day(now: float) -> list:
    t = time.gmtime(now)
    return [t.tm_year, t.tm_yday]


class ResumeBudget:
    """A per-UTC-day fire counter: the runaway-chain backstop.

    ``take(now)`` returns True and counts a fire when the day is under ``cap``,
    else False. The count resets when the UTC day rolls over. A non-positive
    cap never allows a fire (disables autonomous resume even with the master
    flag on). Bot-only (single process), but uses the same atomic write as the
    other stores so a crash never leaves a torn file.
    """

    def __init__(self, path: str | os.PathLike[str], cap: int):
        self._store = JsonDictStore(path, "resume budget", sort_keys=True)
        self.path = self._store.path
        self.cap = cap

    @contextmanager
    def _locked(self):
        with self._store.locked():
            yield

    def _load(self) -> dict:
        return self._store.load()

    def _save(self, state: dict) -> None:
        self._store.save(state)

    def take(self, now: float) -> bool:
        if self.cap <= 0:
            return False
        with self._locked():
            state = self._load()
            day = _utc_day(now)
            count = int(state.get("count", 0)) if state.get("day") == day else 0
            if count >= self.cap:
                # Reaching this branch means state["day"] == day and count is
                # already at the cap, so the file holds the right values; no save.
                return False
            state["day"], state["count"] = day, count + 1
            self._save(state)
            return True


def dispatch_resumes(queue: ResumeQueue, budget: ResumeBudget, *, now: float,
                     parked: bool, submit) -> int:
    """Drain the queue and fire each accepted request. Returns the count fired.

    A request is fired by calling ``submit(conversation_id, prompt)``. Requests
    are dropped (not re-queued) when the credit guard is parked or the daily
    budget is spent — the ordinary fold note is already in the inbox, so the
    owner's next message still surfaces the result; a stale auto-continuation
    firing hours late would be worse than waiting. Malformed entries are
    skipped. The whole drain is one atomic take, so a crash mid-dispatch cannot
    replay already-handled requests.
    """
    items = queue.drain()
    fired = 0
    budget_exhausted = False
    for item in items:
        conversation_id = item.get("conversation_id")
        prompt = item.get("prompt")
        if not conversation_id or not prompt:
            continue
        if parked or budget_exhausted:
            continue
        if not budget.take(now):
            # The day's cap is spent; stop hitting the budget store for the
            # rest of this batch (every further take would just relock and
            # refuse). The dropped requests still sit in the fold-back inbox.
            budget_exhausted = True
            continue
        submit(conversation_id, prompt)
        fired += 1
    return fired
