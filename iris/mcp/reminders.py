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

from iris.reminders import ReminderStore, fmt_ts, parse_when

STORE = ReminderStore(os.environ.get("IRIS_REMINDERS_FILE", "iris-reminders.json"))
DEFAULT_CHANNEL = os.environ.get("IRIS_DISCORD_HOME_CHANNEL", "")

mcp = FastMCP("iris-reminders")


@mcp.tool()
def schedule_reminder(text: str, when: str, channel_id: Optional[str] = None) -> str:
    """Schedule a reminder message to be delivered later.

    Args:
        text: The reminder to send.
        when: When to send it: +30m, +2h, +1d, or an ISO datetime (UTC).
        channel_id: Channel to send to; defaults to the home channel.
    """
    channel = channel_id or DEFAULT_CHANNEL
    if not channel:
        return "No channel to send to (set IRIS_DISCORD_HOME_CHANNEL or pass channel_id)."
    try:
        due = parse_when(when)
    except ValueError as exc:
        return str(exc)
    reminder_id = STORE.add(due, text, channel)
    return f"Reminder #{reminder_id} set for {fmt_ts(due)}: {text}"


@mcp.tool()
def list_reminders() -> str:
    """List the pending reminders."""
    items = STORE.all()
    if not items:
        return "No reminders scheduled."
    return "\n".join(f"#{i['id']} at {fmt_ts(i['due_ts'])}: {i['text']}" for i in items)


@mcp.tool()
def cancel_reminder(reminder_id: int) -> str:
    """Cancel a pending reminder by id."""
    return f"Cancelled reminder #{reminder_id}." if STORE.remove(reminder_id) else f"No reminder #{reminder_id}."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
