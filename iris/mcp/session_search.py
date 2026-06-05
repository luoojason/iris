"""MCP server: search the agent's own past conversations.

Claude Code already persists every session as JSONL under ~/.claude/projects, so
this indexes nothing new; it just reads those transcripts so the agent can recall
what was said days ago, beyond the rolling window of one ``--resume`` session.
Scoped to the local transcript files (``IRIS_TRANSCRIPTS_DIR``).
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path
from typing import Iterator

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Needs the MCP SDK: pip install 'iris-agent[memory]'") from exc

TRANSCRIPTS = os.environ.get("IRIS_TRANSCRIPTS_DIR") or str(Path.home() / ".claude" / "projects")

mcp = FastMCP("iris-history")


def _transcript_files(limit: int = 200) -> list[str]:
    files = glob.glob(os.path.join(TRANSCRIPTS, "**", "*.jsonl"), recursive=True)
    files.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    return files[:limit]


def _messages(files: list[str]) -> Iterator[tuple[str, str, str]]:
    """Yield (role, text, timestamp) for each text message in the files."""
    for path in files:
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    role, text, ts = _extract(obj)
                    if text:
                        yield role, text, ts
        except OSError:
            continue


def _extract(obj: dict) -> tuple[str, str, str]:
    message = obj.get("message") or {}
    role = message.get("role") or obj.get("type") or "?"
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        text = " ".join(parts)
    else:
        text = ""
    return role, text.strip(), obj.get("timestamp", "")


def _line(role: str, text: str, ts: str) -> str:
    return f"[{ts}] {role}: {text.replace(chr(10), ' ')[:200]}"


@mcp.tool()
def search_history(query: str, limit: int = 15) -> str:
    """Search your past conversations for a word or phrase (most recent files first).

    Args:
        query: Text to look for in past messages.
        limit: Maximum matches to return.
    """
    needle = query.lower()
    hits = []
    for role, text, ts in _messages(_transcript_files()):
        if needle in text.lower():
            hits.append(_line(role, text, ts))
            if len(hits) >= max(1, limit):
                break
    return "\n".join(hits) if hits else f"No past messages mention '{query}'."


@mcp.tool()
def recent_history(limit: int = 15) -> str:
    """Show the most recent messages from your latest conversation."""
    files = _transcript_files(limit=1)
    if not files:
        return "(no conversation history yet)"
    rows = list(_messages(files))
    out = [_line(r, t, ts) for r, t, ts in rows[-max(1, limit):]]
    return "\n".join(out) or "(no history yet)"


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
