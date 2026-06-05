"""MCP server: scoped Discord server actions.

Gives the agent the Discord operations the message adapter does not do on its own
(start a thread, look back at history, list channels, find a member), as a narrow
audited tool surface rather than raw shell. It calls the Discord REST API with the
bot token; there is deliberately no arbitrary "send to any channel" tool, since the
bot already replies through the adapter.

Token comes from ``IRIS_DISCORD_TOKEN`` (or ``DISCORD_BOT_TOKEN``). Channel and
guild default to ``IRIS_DISCORD_HOME_CHANNEL`` / ``IRIS_DISCORD_GUILD_ID`` when not
given, so the agent can just call ``create_thread("name")``.

Wire it in next to the memory server in your mcp config, and allowlist its tools.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Needs the MCP SDK: pip install 'iris-agent[memory]'") from exc

API = "https://discord.com/api/v10"
mcp = FastMCP("iris-discord")


def _token() -> str:
    return os.environ.get("IRIS_DISCORD_TOKEN") or os.environ.get("DISCORD_BOT_TOKEN", "")


def _home_channel(given: Optional[str]) -> str:
    return given or os.environ.get("IRIS_DISCORD_HOME_CHANNEL", "")


def _guild(given: Optional[str]) -> str:
    return given or os.environ.get("IRIS_DISCORD_GUILD_ID", "")


def discord_request(method: str, path: str, body: Optional[dict] = None):
    """Make a Discord REST call. Returns parsed JSON or an {'error': ...} dict."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={
            "Authorization": f"Bot {_token()}",
            "Content-Type": "application/json",
            "User-Agent": "iris (https://github.com/luoojason/iris, 0.1)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}", "detail": exc.read().decode("utf-8", "replace")[:300]}
    except Exception as exc:  # network, timeout
        return {"error": str(exc)}


def _is_err(res) -> bool:
    return isinstance(res, dict) and "error" in res


@mcp.tool()
def create_thread(name: str, channel_id: Optional[str] = None) -> str:
    """Start a public thread in a channel (defaults to the home channel).

    Args:
        name: The thread title.
        channel_id: Channel to create it in; defaults to the home channel.
    """
    channel = _home_channel(channel_id)
    if not channel:
        return "No channel id given and no home channel configured."
    res = discord_request("POST", f"/channels/{channel}/threads",
                          {"name": name[:100], "type": 11, "auto_archive_duration": 1440})
    if _is_err(res):
        return f"Could not create thread: {res['error']} {res.get('detail', '')}".strip()
    return f"Created thread '{res.get('name')}' (id {res.get('id')})."


@mcp.tool()
def fetch_messages(channel_id: Optional[str] = None, limit: int = 20) -> str:
    """Read recent messages from a channel (most recent first; defaults to home)."""
    channel = _home_channel(channel_id)
    if not channel:
        return "No channel id given and no home channel configured."
    res = discord_request("GET", f"/channels/{channel}/messages?limit={min(max(limit, 1), 50)}")
    if _is_err(res):
        return f"Could not fetch messages: {res['error']}"
    lines = [f"{m.get('author', {}).get('username', '?')}: {(m.get('content') or '')[:200]}" for m in (res or [])]
    return "\n".join(lines) or "(no messages)"


@mcp.tool()
def list_channels(guild_id: Optional[str] = None) -> str:
    """List the text channels in a guild (server)."""
    guild = _guild(guild_id)
    if not guild:
        return "No guild id given and no guild configured."
    res = discord_request("GET", f"/guilds/{guild}/channels")
    if _is_err(res):
        return f"Could not list channels: {res['error']}"
    text = [f"#{c.get('name')} (id {c.get('id')})" for c in (res or []) if c.get("type") in (0, 5)]
    return "\n".join(text) or "(no text channels)"


@mcp.tool()
def search_members(query: str, guild_id: Optional[str] = None, limit: int = 10) -> str:
    """Find guild members whose name starts with a query."""
    guild = _guild(guild_id)
    if not guild:
        return "No guild id given and no guild configured."
    q = urllib.parse.quote(query)
    res = discord_request("GET", f"/guilds/{guild}/members/search?query={q}&limit={min(max(limit, 1), 20)}")
    if _is_err(res):
        return f"Could not search members: {res['error']}"
    out = [f"{m.get('user', {}).get('username', '?')} (id {m.get('user', {}).get('id')})" for m in (res or [])]
    return "\n".join(out) or "(no members found)"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
