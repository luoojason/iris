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
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

_REL = re.compile(r"^\+(\d+)\s*([mhd])$", re.IGNORECASE)
_UNIT = {"m": 60, "h": 3600, "d": 86400}


def parse_when(when: str, now: Optional[float] = None) -> float:
    """Resolve '+30m' / '+2h' / '+1d' or an ISO datetime to an epoch timestamp."""
    now = time.time() if now is None else now
    text = (when or "").strip()
    rel = _REL.match(text)
    if rel:
        return now + int(rel.group(1)) * _UNIT[rel.group(2).lower()]
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"could not parse time {when!r}; use +30m, +2h, +1d, or an ISO datetime") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class ReminderStore:
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
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(items, handle, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def add(self, due_ts: float, text: str, channel_id: str) -> int:
        with self._locked():
            items = self._load()
            new_id = max((int(i.get("id", 0)) for i in items), default=0) + 1
            items.append({"id": new_id, "due_ts": due_ts, "text": text, "channel_id": channel_id})
            self._save(items)
        return new_id

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
        """Atomically remove and return all jobs due at or before ``now``."""
        now = time.time() if now is None else now
        with self._locked():
            items = self._load()
            due = [i for i in items if i.get("due_ts", 0) <= now]
            if due:
                self._save([i for i in items if i.get("due_ts", 0) > now])
            return due


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
