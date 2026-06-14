"""MCP server: let the agent record, list, and cancel standing goals.

The agent only writes goals here; advancing them happens out of band via
``python -m iris goal-tick`` (cron), so no model call is ever made on a clock by
this server. A goal is scoped to the thread it was set in (IRIS_ORIGIN_CHANNEL,
plumbed by the driver) so the goal tick's progress reports route back to where
the owner asked, falling back to the home channel.
"""

from __future__ import annotations

import os
from typing import Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Needs the MCP SDK: pip install 'iris-agent[memory]'") from exc

from iris.goals import GoalStore

# All of these are read LAZILY (via load_dotenv at call time), never at import:
# the claude child strips IRIS_* from this server's spawn env, so an import-time
# os.environ read would miss a customized IRIS_GOALS_FILE / home channel and the
# tool would silently write a different file than the `iris goal-tick` cron reads.
# The module globals stay as a None-default test override seam (the test sets a
# concrete value and the lazy path is skipped).
STORE: Optional[GoalStore] = None
DEFAULT_CHANNEL: Optional[str] = None
# A ceiling on simultaneously-active goals so a runaway turn (or prompt injection)
# cannot fill the loop with work.
MAX_ACTIVE: Optional[int] = None

mcp = FastMCP("iris-goals")


def _store() -> GoalStore:
    if STORE is not None:
        return STORE
    from iris.config import load_dotenv

    load_dotenv()
    return GoalStore(os.environ.get("IRIS_GOALS_FILE", "iris-goals.json"))


def _default_channel() -> str:
    if DEFAULT_CHANNEL is not None:
        return DEFAULT_CHANNEL
    from iris.config import load_dotenv

    load_dotenv()
    return os.environ.get("IRIS_DISCORD_HOME_CHANNEL", "")


def _max_active() -> int:
    if MAX_ACTIVE is not None:
        return MAX_ACTIVE
    from iris.config import load_dotenv

    load_dotenv()
    return int(os.environ.get("IRIS_GOALS_MAX_ACTIVE", "10"))


def _default_max_steps() -> int:
    from iris.config import load_dotenv

    load_dotenv()
    return int(os.environ.get("IRIS_GOALS_MAX_STEPS", "20"))


@mcp.tool()
def set_goal(text: str, max_steps: Optional[int] = None) -> str:
    """Record a standing goal you will keep advancing on your own until it is done.

    Use this when Jason gives you an objective to pursue over time, not a one-off
    task. A background tick will advance the goal one step at a time (only while
    there's usage headroom), check progress with an independent judge, and ping
    you here when it is done or needs a decision.

    Args:
        text: The goal, stated as an outcome to reach.
        max_steps: Optional cap on how many work steps to spend before stopping to
            ask you (defaults to IRIS_GOALS_MAX_STEPS).
    """
    text = (text or "").strip()
    if not text:
        return "A goal needs a description."
    store = _store()
    active = store.active()
    if len(active) >= _max_active():
        return (f"You already have {len(active)} active goals, the most allowed. "
                "Cancel one with cancel_goal before adding another.")
    # IRIS_ORIGIN_CHANNEL is added back AFTER the IRIS_* strip by the driver, so it
    # is present here; it is the thread this goal was set in, so reports route back.
    origin = os.environ.get("IRIS_ORIGIN_CHANNEL") or _default_channel()
    conversation_id = f"discord:{origin}" if origin else None
    steps = int(max_steps) if max_steps else _default_max_steps()
    goal = store.add(text, conversation_id=conversation_id, max_steps=steps)
    return (f"Goal #{goal['id']} set: {text}\n"
            f"I'll advance it on my own (up to {steps} steps) and report back here.")


@mcp.tool()
def list_goals() -> str:
    """List your goals: the active ones you're pursuing, and recent outcomes."""
    items = _store().all()
    if not items:
        return "No goals set."
    lines = []
    for g in items:
        if g.get("status") == "active":
            lines.append(f"#{g['id']} [active {g.get('steps', 0)}/{g.get('max_steps', '?')}]: {g['text']}")
        else:
            lines.append(f"#{g['id']} [{g.get('status')}]: {g['text']}")
    return "\n".join(lines)


@mcp.tool()
def cancel_goal(goal_id: int) -> str:
    """Cancel a goal by id so the tick stops advancing it."""
    import time

    store = _store()
    goal = store.get(goal_id)
    if goal is None:
        return f"No goal #{goal_id}."
    store.transition(goal_id, "cancelled", time.time())
    return f"Cancelled goal #{goal_id}: {goal['text']}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
