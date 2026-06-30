"""The morning briefing: a model-free status read over Iris's autonomy state.

Everything Iris runs on the clock (reminders, goals, schedules, the heartbeat,
the fold-back inbox) already keeps its state in a plain JSON store. This module
just reads those stores and folds them into one short status block, so the owner
(or an owner-authored schedule that pipes the text to Discord) can see where
things stand in a glance.

It is deliberately the cheap path: ZERO model calls, no network. The heartbeat
section runs the same level-triggered checks the tick does, but with a no-op URL
fetcher injected so a url check never reaches out; every other read is a local
file load. A briefing is a read, never a mutation, so it drains nothing and fires
nothing - draining the inbox here would eat notes the next chat turn is owed.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import heartbeat
from .goals import GoalStore, format_goal_line
from .reminders import ReminderStore, fmt_ts
from .schedules import ScheduleStore, describe_rule
from .statefile import JsonDictStore, JsonListStore
from .usage import month_key

# How many items to spell out per section before collapsing to just the count.
_MAX_LISTED = 3


def _reminders_section(now: float) -> Optional[str]:
    """Reminders due by the end of today (UTC), overdue ones included.

    The reminders file path is an env var, not a Config field, matching how the
    CLI tick and the TUI locate it (``IRIS_REMINDERS_FILE``).
    """
    path = os.environ.get("IRIS_REMINDERS_FILE", "iris-reminders.json")
    day_start = datetime.fromtimestamp(now, timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0)
    end_of_today = (day_start + timedelta(days=1)).timestamp()  # midnight tonight UTC
    due = [r for r in ReminderStore(path).all() if r.get("due_ts", 0) <= end_of_today]
    if not due:
        return None
    lines = [f"Reminders due by end of today: {len(due)}"]
    for r in due[:_MAX_LISTED]:
        lines.append(f"  - {fmt_ts(r.get('due_ts', 0))}: {r.get('text', '')}")
    if len(due) > _MAX_LISTED:
        lines.append(f"  - ...and {len(due) - _MAX_LISTED} more")
    return "\n".join(lines)


def _goals_section(config) -> Optional[str]:
    """Active standing goals, each at its current N/max step count."""
    active = GoalStore(config.goals_file).active()
    if not active:
        return None
    lines = [f"Active goals: {len(active)}"]
    for g in active[:_MAX_LISTED]:
        lines.append(f"  - {format_goal_line(g)}")
    if len(active) > _MAX_LISTED:
        lines.append(f"  - ...and {len(active) - _MAX_LISTED} more")
    return "\n".join(lines)


def _schedules_section(config, now: float) -> Optional[str]:
    """The soonest enabled schedule fire, plus how many fired this month."""
    rules = [r for r in ScheduleStore(config.schedules_file).all()
             if r.get("enabled", True) and isinstance(r.get("due_ts"), (int, float))]
    if not rules:
        return None
    soonest = min(rules, key=lambda r: r.get("due_ts", 0))
    mkey = month_key(now)
    fired = 0
    for r in rules:
        try:
            fired += int((r.get("fired") or {}).get(mkey, 0))
        except (TypeError, ValueError):
            continue
    return (f"Next schedule: {describe_rule(soonest, now)}\n"
            f"  Fired this month: {fired}")


def _heartbeat_section(config, now: float) -> Optional[str]:
    """Currently failing heartbeat checks (no network: url checks see a 200)."""
    failures, _skipped, _total = heartbeat.evaluate_all(
        config, now, fetch=lambda url, timeout: 200)
    if not failures:
        return None
    lines = [f"Heartbeat: {len(failures)} check(s) failing:"]
    for name, detail in sorted(failures.items()):
        lines.append(f"  - {name}: {detail}")
    return "\n".join(lines)


def _waiting_section(config) -> Optional[str]:
    """Pending fold-back inbox notes, and approvals awaiting an owner tap."""
    lines: list[str] = []
    notes = JsonListStore(config.inbox_file, "fold-back inbox").load()
    if notes:
        lines.append(f"Inbox: {len(notes)} pending note(s)")
    approvals_file = getattr(config, "approvals_file", "")
    if approvals_file:
        data = JsonDictStore(approvals_file, "approvals").load()
        pending = sum(1 for v in data.values()
                      if isinstance(v, dict) and v.get("decision") is None)
        if pending:
            lines.append(f"Approvals: {pending} awaiting your tap")
    return "\n".join(lines) if lines else None


def build_briefing(config, now: Optional[float] = None) -> str:
    """Assemble a one-block status read over Iris's autonomy state. No model call.

    Reads the reminder, goal, schedule, heartbeat, inbox, and approval stores and
    folds the non-empty ones into a short text block: reminders due by end of
    today, active goals at N/max steps, the next schedule fire and this month's
    fire count, any failing heartbeat checks, and pending inbox notes / approvals.
    Empty sections are omitted; an all-empty state returns one friendly quiet line.
    """
    now = time.time() if now is None else now
    sections = [
        _reminders_section(now),
        _goals_section(config),
        _schedules_section(config, now),
        _heartbeat_section(config, now),
        _waiting_section(config),
    ]
    body = [s for s in sections if s]
    if not body:
        return "All quiet: nothing due, no active goals, and nothing waiting on you."
    header = f"Morning briefing for {datetime.fromtimestamp(now, timezone.utc):%Y-%m-%d} UTC:"
    return "\n\n".join([header, *body])
