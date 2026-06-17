"""MCP server: let the agent PROPOSE changes to its own skills, for approval.

A skill is an instruction the model follows, so rewriting one is the highest-
stakes self-modification Iris can do. This server only ever STAGES a proposal;
it is applied to the live skills directory solely by an explicit owner action
(`iris skills approve <id>`), never from a model turn. The maintain proactive
review uses this to turn "what I learned" into a concrete, reviewable proposal.
"""

from __future__ import annotations

import os
from typing import Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Needs the MCP SDK: pip install 'iris-agent[memory]'") from exc

from iris.skills import SkillProposalStore, validate_skill

STORE: Optional[SkillProposalStore] = None
# Most pending proposals at once, so a runaway turn can't flood the staging area.
# None -> read the env lazily (the claude child strips IRIS_* from this server).
MAX_PENDING: Optional[int] = None

mcp = FastMCP("iris-skills")


def _store() -> SkillProposalStore:
    if STORE is not None:
        return STORE
    from iris.config import load_dotenv

    load_dotenv()
    return SkillProposalStore(os.environ.get("IRIS_SKILL_PROPOSALS_FILE", "iris-skill-proposals.json"))


def _max_pending() -> int:
    if MAX_PENDING is not None:
        return MAX_PENDING
    from iris.config import load_dotenv

    load_dotenv()
    try:
        return int(os.environ.get("IRIS_SKILL_PROPOSALS_MAX", "10"))
    except ValueError:
        return 10  # a non-numeric override must not break the tool


@mcp.tool()
def propose_skill(name: str, content: str, rationale: str, kind: str = "edit") -> str:
    """Propose a new or revised skill for the owner to review and approve.

    Use this when you've learned something durable about how you should work and
    want to write it down as a skill. The proposal is STAGED only — it does not
    change your behavior until Jason approves it with `iris skills approve <id>`.

    Args:
        name: A lowercase slug for the skill folder (e.g. 'summarize-threads').
        content: The full SKILL.md text, including frontmatter with a 'description:'.
        rationale: One or two lines on why this skill is worth adding/changing.
        kind: 'new' for a brand-new skill, 'edit' to revise an existing one.
    """
    error = validate_skill(name, content)
    if error:
        return error
    store = _store()
    if len(store.pending()) >= _max_pending():
        return ("You already have the most pending skill proposals allowed. Ask Jason "
                "to approve or reject some (`iris skills pending`) before proposing more.")
    proposal = store.add(name, content, rationale, kind=(kind or "edit").strip().lower())
    return (f"Proposed skill #{proposal['id']} ('{name}'), staged for review. It will "
            f"NOT change my behavior until you run `iris skills approve {proposal['id']}` "
            f"(or `iris skills reject {proposal['id']}`).")


@mcp.tool()
def list_skill_proposals() -> str:
    """List your staged skill proposals and their status."""
    items = _store().all()
    if not items:
        return "No skill proposals."
    lines = []
    for p in items:
        lines.append(f"#{p['id']} [{p.get('status')}] {p.get('kind')} '{p['name']}': "
                     f"{(p.get('rationale') or '').strip()[:120]}")
    return "\n".join(lines)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
