"""Instant, zero-inference per-conversation recap (parity item CM-3).

`!recap` (Discord) and `iris recap` (CLI) render a quick local summary of a
single Claude Code transcript: how many turns each side took, which tools ran,
which files were edited, and the last ask and reply. Unlike `digest`, no model
is ever called; this only reads and formats the JSONL transcript that Claude
Code already wrote, using the same files `session_search` and `digest` read.

A transcript is one JSON object per line. Each line carries a `message` with a
`role` ("user"/"assistant") and `content` that is either a string or a list of
blocks; a `text` block has `text`, and a `tool_use` block has `name` and
`input` (Edit/Write carry `input.file_path`). The parser tolerates malformed
lines and missing fields rather than raising.
"""

from __future__ import annotations

import glob
import json
import os
from pathlib import Path
from typing import Iterable, Optional

# Raw Claude Code tool names folded to friendly verbs. Unmapped tools keep their
# raw name, so only the common ones need an entry here.
FRIENDLY_TOOLS = {
    "Edit": "edited files",
    "Write": "wrote files",
    "Read": "read files",
    "Bash": "ran commands",
    "Glob": "searched for files",
    "Grep": "searched file contents",
    "Task": "delegated to a subagent",
    "WebFetch": "fetched web pages",
    "WebSearch": "searched the web",
    "TodoWrite": "updated the to-do list",
}

# Tools whose `input.file_path` names a file the agent changed on disk.
_EDIT_TOOLS = frozenset({"Edit", "Write"})

# How much of the last assistant reply to keep before truncating.
REPLY_TRUNCATE = 280


def _text_of(content) -> str:
    """The concatenated text of a message's content (string or block list)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(p for p in parts if p).strip()
    return ""


def _tool_uses(content):
    """Yield (name, input_dict) for each tool_use block in a content list."""
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_use":
            name = block.get("name")
            if isinstance(name, str) and name:
                inp = block.get("input")
                yield name, inp if isinstance(inp, dict) else {}


def _oneline(text: str, limit: int = 200) -> str:
    """Collapse whitespace and truncate to ``limit`` characters for display."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def parse_transcript(lines: Iterable[str]) -> dict:
    """Summarize Claude Code transcript JSONL ``lines`` for a local recap.

    Pure and defensive: non-strings, blank lines, malformed JSON, and missing
    fields are skipped, never raised. Returns a dict with visible user and
    assistant turn counts (a turn is a message that carries text, so tool-result
    echoes do not inflate the count), the tools used (folded to friendly verbs,
    mapped to call counts), recently-edited file paths (most recent last), and
    the last user ask and last assistant reply (truncated).
    """
    user_turns = 0
    assistant_turns = 0
    tools: dict[str, int] = {}
    edited: list[str] = []
    last_user = ""
    last_assistant = ""

    for line in lines:
        if not isinstance(line, str):
            continue
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue

        message = obj.get("message")
        if not isinstance(message, dict):
            message = {}
        role = message.get("role") or obj.get("type") or ""
        content = message.get("content")
        text = _text_of(content)

        for name, inp in _tool_uses(content):
            friendly = FRIENDLY_TOOLS.get(name, name)
            tools[friendly] = tools.get(friendly, 0) + 1
            if name in _EDIT_TOOLS:
                path = inp.get("file_path")
                if isinstance(path, str) and path:
                    if path in edited:  # keep the recency order, most recent last
                        edited.remove(path)
                    edited.append(path)

        if role == "user" and text:
            user_turns += 1
            last_user = text
        elif role == "assistant" and text:
            assistant_turns += 1
            last_assistant = text

    return {
        "user_turns": user_turns,
        "assistant_turns": assistant_turns,
        "tools": tools,
        "edited_files": edited,
        "last_user": last_user,
        "last_assistant": last_assistant[:REPLY_TRUNCATE],
    }


def build_recap(path: str) -> str:
    """Render a short, zero-inference recap of the transcript file at ``path``.

    Reads the JSONL transcript and formats :func:`parse_transcript` into a few
    human-readable lines. No model is ever called. Returns a brief notice when
    the file is missing or has nothing worth recapping.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = parse_transcript(handle)
    except OSError:
        return "(no transcript to recap)"

    if not (data["user_turns"] or data["assistant_turns"] or data["tools"]):
        return "(nothing to recap in this conversation yet)"

    lines = [f"Turns: {data['user_turns']} from you, {data['assistant_turns']} from me."]

    if data["tools"]:
        top = sorted(data["tools"].items(), key=lambda kv: kv[1], reverse=True)[:5]
        lines.append("Did: " + ", ".join(f"{name} (x{count})" for name, count in top))

    if data["edited_files"]:
        recent = list(reversed(data["edited_files"]))[:5]
        lines.append("Edited: " + ", ".join(recent))

    if data["last_user"]:
        lines.append("Last ask: " + _oneline(data["last_user"]))

    if data["last_assistant"]:
        lines.append("Last reply: " + _oneline(data["last_assistant"]))

    return "\n".join(lines)


def latest_transcript(transcripts_dir: Optional[str] = None) -> Optional[str]:
    """The most-recently-modified ``*.jsonl`` transcript path, or None if none.

    Uses the same recursive glob as :mod:`iris.mcp.session_search` and
    :mod:`iris.digest`, defaulting to ``IRIS_TRANSCRIPTS_DIR`` or
    ``~/.claude/projects``.
    """
    tdir = transcripts_dir or os.environ.get("IRIS_TRANSCRIPTS_DIR") or str(
        Path.home() / ".claude" / "projects")
    files = [f for f in glob.glob(os.path.join(tdir, "**", "*.jsonl"), recursive=True)
             if os.path.exists(f)]
    if not files:
        return None
    return max(files, key=os.path.getmtime)
