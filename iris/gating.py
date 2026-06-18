"""Per-context tool gating for the clock-triggered contexts.

The clock may run an owner-authored review (proactive) or advance an
owner-authored goal, but it must never create NEW self-starting work — a new
schedule, goal, or background job — or it could amplify itself off the clock.
Chat keeps the full toolset (the owner's control plane); only the clock contexts
are gated. Jobs are already isolated separately (they get no iris MCP servers).
"""

from __future__ import annotations

from dataclasses import replace

from .config import Config
from .driver import DANGEROUS_BUILTINS

# The MCP tools that CREATE new clock- or self-triggered inference. Stripped from
# the clock contexts so a timer-started turn cannot spin up more timer-started work.
SELF_STARTING_TOOLS = (
    "mcp__jobs__schedule_job",
    "mcp__jobs__run_in_background",
    "mcp__jobs__start_job",
    "mcp__goals__set_goal",
)


def gate_self_starting(config: Config) -> Config:
    """Return a config copy with the self-starting-work tools removed, for use by
    the clock-triggered contexts (proactive reviews, goal ticks).

    Gates two ways: the tools are dropped from the allowlist (the primary
    boundary) and added to the denylist (so an empty/absent allowlist still can't
    reach them). The existing chat denylist — the dangerous built-ins the driver
    would otherwise apply — is preserved, never replaced.
    """
    allowed = [t for t in config.allowed_tools if t not in SELF_STARTING_TOOLS]
    if config.disallowed_tools:
        base_deny = list(config.disallowed_tools)
    elif getattr(config, "restrict_builtin_tools", True):
        base_deny = list(DANGEROUS_BUILTINS)
    else:
        base_deny = []
    disallowed = base_deny + [t for t in SELF_STARTING_TOOLS if t not in base_deny]
    return replace(config, allowed_tools=allowed, disallowed_tools=disallowed)
