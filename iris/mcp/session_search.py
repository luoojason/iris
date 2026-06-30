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
import re
from pathlib import Path
from typing import Iterator

_WORD = re.compile(r"[a-z0-9]+")


def _terms(query: str) -> list[str]:
    """Query tokens worth matching on (lowercased words, single chars dropped)."""
    return [t for t in _WORD.findall((query or "").lower()) if len(t) > 1]


def _score(text: str, terms: list[str]) -> int:
    """Relevance of one message to the query terms (word-based, 0 = no match).

    A message that contains MORE distinct query terms always outranks one that
    repeats a single term, so "weekly usage cap" surfaces the message about all
    three over one that just says "cap" ten times. Zero when no term matches, so a
    query whose words appear nowhere returns nothing rather than the newest noise.
    """
    if not terms:
        return 0
    counts: dict = {}
    for word in _WORD.findall(text.lower()):
        counts[word] = counts.get(word, 0) + 1
    matched = sum(1 for t in terms if t in counts)
    if matched == 0:
        return 0
    occurrences = sum(counts.get(t, 0) for t in terms)
    return matched * 1000 + occurrences


def rank_history(messages, query: str, limit: int = 15) -> list:
    """Rank (role, text, ts) messages by relevance to the query; best first.

    Pure and model-free. ``messages`` arrive newest-first, so a stable sort keeps
    recency as the tiebreak among equally-relevant hits.
    """
    terms = _terms(query)
    if not terms:
        return []
    scored = []
    for index, (role, text, ts) in enumerate(messages):
        s = _score(text, terms)
        if s > 0:
            scored.append((s, index, role, text, ts))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [(role, text, ts) for _, _, role, text, ts in scored[: max(1, limit)]]

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
    ranked = rank_history(list(_messages(_transcript_files())), query, limit)
    if not ranked:
        return f"No past messages mention '{query}'."
    return "\n".join(_line(role, text, ts) for role, text, ts in ranked)


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
