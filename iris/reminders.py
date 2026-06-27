"""Scheduled reminders: store, time parsing, and the outbound sender.

The agent schedules a reminder (an MCP tool writes a job to a JSON file). A
separate periodic tick (``python -m iris reminders-tick``, run from cron or a
systemd timer) reads due jobs and delivers them. The model is never called on a
clock; one delivery is one event, so this keeps the zero-idle-inference shape
that keeps Iris inside the subscription's metered budget.

Delivery is a plain Discord REST post, not an agent tool, so the agent can
schedule but cannot send to arbitrary channels.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from .statefile import JsonListStore

_REL = re.compile(r"^\+(\d+)\s*([mhd])$", re.IGNORECASE)
_EVERY = re.compile(r"^(?:every\s+)?(\d+)\s*([mhd])$", re.IGNORECASE)
_UNIT = {"m": 60, "h": 3600, "d": 86400}


def parse_when(when: str, now: Optional[float] = None) -> float:
    """Resolve '+30m' / '+2h' / '+1d' or an ISO datetime to an epoch timestamp."""
    now = time.time() if now is None else now
    text = (when or "").strip()
    rel = _REL.match(text)
    if rel:
        return now + int(rel.group(1)) * _UNIT[rel.group(2).lower()]
    # datetime.fromisoformat did not accept the 'Z' UTC suffix until Python
    # 3.11; normalize it so the 3.10 floor parses ISO timestamps too.
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError as exc:
        raise ValueError(f"could not parse time {when!r}; use +30m, +2h, +1d, or an ISO datetime") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def parse_every(every: str) -> int:
    """Resolve a recurrence like 'every 1d' / '2h' / '30m' to a period in seconds.

    A bare unit string ('1d') works too, so 'every' is optional sugar. Returns 0
    for an empty spec (a one-shot reminder); raises on anything unparseable.
    """
    text = (every or "").strip()
    if not text:
        return 0
    match = _EVERY.match(text)
    if not match:
        raise ValueError(f"could not parse recurrence {every!r}; use every 30m, every 2h, or every 1d")
    seconds = int(match.group(1)) * _UNIT[match.group(2).lower()]
    if seconds <= 0:
        raise ValueError("a recurrence must be a positive interval")
    return seconds


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class ReminderStore:
    def __init__(self, path: str | os.PathLike[str]):
        self._store = JsonListStore(path, "reminders")
        self.path = self._store.path

    @contextmanager
    def _locked(self):
        with self._store.locked():
            yield

    def _load(self) -> list[dict]:
        return self._store.load()

    def _save(self, items: list[dict]) -> None:
        self._store.save(items)

    def add(self, due_ts: float, text: str, channel_id: str, repeat_secs: int = 0,
            kind: str = "", origin: str = "") -> int:
        with self._locked():
            items = self._load()
            new_id = max((int(i.get("id", 0)) for i in items), default=0) + 1
            record = {
                "id": new_id, "due_ts": due_ts, "text": text,
                "channel_id": channel_id, "repeat_secs": int(repeat_secs or 0),
            }
            # Optional identity fields, stored only when set so plain reminders
            # keep the original record shape.
            if kind:
                record["kind"] = kind
            if origin:
                record["origin"] = origin
            items.append(record)
            self._save(items)
        return new_id

    def requeue(self, job: dict) -> int:
        """Re-add a failed delivery as a one-shot, preserving who and what it was.

        The next occurrence of a recurring job was already rescheduled by
        :meth:`pop_due`, so the failed firing itself goes back without a repeat,
        to be retried on the next tick.
        """
        return self.add(
            job.get("due_ts", 0), job.get("text", ""), job.get("channel_id", ""),
            kind=job.get("kind", ""), origin=job.get("origin", ""),
        )

    def all(self) -> list[dict]:
        return sorted(self._load(), key=lambda i: i.get("due_ts", 0))

    def remove(self, reminder_id: int) -> bool:
        with self._locked():
            items = self._load()
            kept = [i for i in items if i.get("id") != reminder_id]
            if len(kept) == len(items):
                return False
            self._save(kept)
            return True

    def pop_due(self, now: Optional[float] = None) -> list[dict]:
        """Atomically take all jobs due at or before ``now`` and return them.

        A one-shot job is removed. A recurring job (``repeat_secs`` > 0) is
        rescheduled in place: it fires once now and its next ``due_ts`` is set
        forward from ``now``, not from its old due time. So a tick that missed a
        window (host asleep, cron skipped) delivers one reminder and resumes the
        cadence, rather than replaying every occurrence it slept through.
        """
        now = time.time() if now is None else now
        with self._locked():
            items = self._load()
            due = [i for i in items if i.get("due_ts", 0) <= now]
            if not due:
                return []
            kept = [i for i in items if i.get("due_ts", 0) > now]
            for job in due:
                period = int(job.get("repeat_secs", 0) or 0)
                if period > 0:
                    nxt = dict(job)
                    nxt["due_ts"] = now + period
                    kept.append(nxt)
            self._save(kept)
            return due


# Kinds the agent may mark on a reminder. A follow-up is a promise the agent
# made during a turn; it renders as such so the owner knows replying resumes it.
KINDS = ("followup",)


def render_reminder(job: dict) -> str:
    """The delivery line for one due reminder (plain text, no model call)."""
    text = job.get("text", "")
    if job.get("kind") == "followup":
        return f"Follow-up I promised: {text} — reply here and I'll pick it up."
    return f"Reminder: {text}"


def send_discord_message(channel_id: str, content: str, token: str) -> bool:
    """Post a message to a Discord channel via REST. Returns success."""
    body = json.dumps({"content": content[:2000]}).encode()
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        data=body, method="POST",
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json",
                 "User-Agent": "iris (https://github.com/luoojason/iris, 0.1)"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20):
            return True
    except (urllib.error.HTTPError, OSError):
        return False
