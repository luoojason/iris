"""MCP server for just-in-time approvals (Claude Code --permission-prompt-tool).

Point the chat driver at this with ``--permission-prompt-tool mcp__approvals__check``
(iris wires it when IRIS_APPROVALS=true). For each permission-needing tool use,
Claude Code calls ``check`` with the tool name and proposed input; this asks the
owner with Approve/Deny buttons in Discord and returns allow/deny, failing closed.

The decision logic (risk policy, rendezvous, poll, fail-closed) lives in
iris/approvals.py and is unit-tested there; this module is the thin glue: read
config, post the buttons over REST, and let the bot's on_interaction record the tap.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from iris.approvals import ApprovalStore, decide

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Needs the MCP SDK: pip install 'iris-agent[memory]'") from exc

API = "https://discord.com/api/v10"
mcp = FastMCP("iris-approvals")


def _post_approval(req_id: str, summary: str, config) -> bool:
    """Post an Approve/Deny prompt to the owner's channel. False if it can't send.

    Prefer the thread this turn ran in (IRIS_ORIGIN_CHANNEL, re-added to the MCP
    server env by the driver) so an approval raised mid-thread appears where the
    owner is working, not over in the home channel; on_interaction records the tap
    by custom_id regardless of channel.
    """
    channel = os.environ.get("IRIS_ORIGIN_CHANNEL") or config.home_channel
    if not channel or not config.discord_token:
        return False
    body = json.dumps({
        "content": f"Approve this action?\n> {summary}",
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 3, "label": "Approve", "custom_id": f"approve:{req_id}"},
            {"type": 2, "style": 4, "label": "Deny", "custom_id": f"deny:{req_id}"},
        ]}],
    }).encode()
    req = urllib.request.Request(
        f"{API}/channels/{channel}/messages", data=body, method="POST",
        headers={"Authorization": f"Bot {config.discord_token}",
                 "Content-Type": "application/json",
                 "User-Agent": "iris-approvals (https://github.com/luoojason/iris, 0.1)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


@mcp.tool()
def check(tool_name: str, input: dict = None) -> str:
    """Permission decision for a proposed tool use (Claude Code permission-prompt-tool)."""
    from iris.config import Config
    config = Config.from_env()
    store = ApprovalStore(config.approvals_file)
    try:
        store.prune(before_ts=time.time() - 24 * 3600)  # keep the file small; best-effort
    except Exception:
        pass
    return decide(
        tool_name, input or {}, config,
        store=store,
        post=lambda rid, summary: _post_approval(rid, summary, config),
        now_fn=time.time, sleep_fn=time.sleep,
        timeout=getattr(config, "approval_timeout", 300.0),
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
