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

STORE = GoalStore(os.environ.get("IRIS_GOALS_FILE", "iris-goals.json"))
DEFAULT_CHANNEL = os.environ.get("IRIS_DISCORD_HOME_CHANNEL", "")
# A ceiling on simultaneously-active goals so a runaway turn (or prompt injection)
# cannot fill the loop with work. None means "read the env lazily": the claude
# child strips IRIS_* from this server's spawn env, so the knob must come from
# .env in the working directory at call time, not import time.
MAX_ACTIVE: Optional[int] = None

mcp = FastMCP("iris-goals")


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
    active = STORE.active()
    if len(active) >= _max_active():
        return (f"You already have {len(active)} active goals, the most allowed. "
                "Cancel one with cancel_goal before adding another.")
    origin = os.environ.get("IRIS_ORIGIN_CHANNEL") or DEFAULT_CHANNEL
    conversation_id = f"discord:{origin}" if origin else None
    steps = int(max_steps) if max_steps else _default_max_steps()
    goal = STORE.add(text, conversation_id=conversation_id, max_steps=steps)
    return (f"Goal #{goal['id']} set: {text}\n"
            f"I'll advance it on my own (up to {steps} steps) and report back here.")


@mcp.tool()
def list_goals() -> str:
    """List your goals: the active ones you're pursuing, and recent outcomes."""
    items = STORE.all()
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

    goal = STORE.get(goal_id)
    if goal is None:
        return f"No goal #{goal_id}."
    STORE.transition(goal_id, "cancelled", time.time())
    return f"Cancelled goal #{goal_id}: {goal['text']}"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
