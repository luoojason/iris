"""MCP server: let the agent schedule, list, and cancel reminders.

The agent only writes jobs here; delivery happens out of band via
``python -m iris reminders-tick`` (cron / systemd timer), so no model call is
ever made on a clock.
"""

from __future__ import annotations

import os
from typing import Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Needs the MCP SDK: pip install 'iris-agent[memory]'") from exc

from iris.reminders import KINDS, ReminderStore, fmt_ts, parse_every, parse_when

STORE: Optional[ReminderStore] = None
DEFAULT_CHANNEL: Optional[str] = None
# A ceiling on how many reminders the agent may have pending at once, so a
# runaway turn (or a prompt-injected loop) cannot fill the schedule. Owner-made
# reminders do not count against it. None means "read the env lazily": the
# claude child strips IRIS_* from this server's spawn env, so the knob must
# come from .env in the working directory at call time, not import time.
MAX_PENDING: Optional[int] = None


def _max_pending() -> int:
    if MAX_PENDING is not None:
        return MAX_PENDING
    from iris.config import load_dotenv

    load_dotenv()
    try:
        return int(os.environ.get("IRIS_REMINDERS_MAX_PENDING", "25"))
    except ValueError:
        return 25  # a non-numeric override must not break the tool


def _store() -> ReminderStore:
    if STORE is not None:
        return STORE
    from iris.config import load_dotenv

    load_dotenv()
    return ReminderStore(os.environ.get("IRIS_REMINDERS_FILE", "iris-reminders.json"))


def _default_channel() -> str:
    if DEFAULT_CHANNEL is not None:
        return DEFAULT_CHANNEL
    from iris.config import load_dotenv

    load_dotenv()
    return os.environ.get("IRIS_DISCORD_HOME_CHANNEL", "")


mcp = FastMCP("iris-reminders")


@mcp.tool()
def schedule_reminder(text: str, when: str, channel_id: Optional[str] = None,
                      every: Optional[str] = None, kind: Optional[str] = None) -> str:
    """Schedule a reminder message to be delivered later, once or on a repeat.

    Args:
        text: The reminder to send.
        when: When to send it (first time): +30m, +2h, +1d, or an ISO datetime (UTC).
        channel_id: Channel to send to; defaults to the home channel.
        every: Optional recurrence: 'every 30m', 'every 2h', 'every 1d'. Omit for
            a one-shot reminder. After each delivery it reschedules from that moment.
        kind: Pass 'followup' when you are scheduling a follow-up to something
            you promised to do later; it is delivered as a follow-up the owner
            can resume by replying. Omit for a plain reminder.
    """
    channel = channel_id or _default_channel()
    if not channel:
        return "No channel to send to (set IRIS_DISCORD_HOME_CHANNEL or pass channel_id)."
    kind = (kind or "").strip().lower()
    if kind and kind not in KINDS:
        return f"Unknown reminder kind {kind!r}; use 'followup' or omit it."
    store = _store()
    pending = sum(1 for item in store.all() if item.get("origin") == "model")
    if pending >= _max_pending():
        return (f"You already have {pending} pending reminders, the most allowed. "
                "Cancel some with cancel_reminder before scheduling more.")
    try:
        due = parse_when(when)
        repeat_secs = parse_every(every or "")
    except ValueError as exc:
        return str(exc)
    reminder_id = store.add(due, text, channel, repeat_secs, kind=kind, origin="model")
    cadence = f", repeating {every.strip()}" if repeat_secs else ""
    label = "Follow-up" if kind == "followup" else "Reminder"
    return f"{label} #{reminder_id} set for {fmt_ts(due)}{cadence}: {text}"


@mcp.tool()
def list_reminders() -> str:
    """List the pending reminders."""
    items = _store().all()
    if not items:
        return "No reminders scheduled."
    lines = []
    for i in items:
        period = int(i.get("repeat_secs", 0) or 0)
        cadence = f" (every {period // 3600}h)" if period and period % 3600 == 0 else (
            f" (every {period // 60}m)" if period else "")
        tag = f" [{i['kind']}]" if i.get("kind") else ""
        lines.append(f"#{i['id']} at {fmt_ts(i['due_ts'])}{cadence}{tag}: {i['text']}")
    return "\n".join(lines)


@mcp.tool()
def cancel_reminder(reminder_id: int) -> str:
    """Cancel a pending reminder by id."""
    return f"Cancelled reminder #{reminder_id}." if _store().remove(reminder_id) else f"No reminder #{reminder_id}."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
