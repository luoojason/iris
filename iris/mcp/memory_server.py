"""A tiny MCP memory server: give the agent durable notes across sessions.

This is an example of how Iris keeps Hermes-style capabilities while the brain
is the official ``claude`` binary. Instead of reimplementing a tool system, we
expose tools the way Claude Code already understands them: as an MCP server.
``claude --mcp-config`` launches this process and the model can call its tools.

Storage is a flat JSON file (``IRIS_MEMORY_FILE``, default ``iris-memory.json``)
so it is trivial to read and back up.

Run standalone for a quick check:
    python -m iris.mcp.memory_server

Wire into Claude via an mcp config file (see examples/mcp.example.json):
    {"mcpServers": {"memory": {"command": "python",
                               "args": ["-m", "iris.mcp.memory_server"]}}}
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - depends on optional extra
    raise SystemExit(
        "The memory tool needs the MCP SDK. Install it with:\n"
        "    pip install mcp\n"
        "or install Iris with the memory extra: pip install 'iris-agent[memory]'"
    ) from exc


MEMORY_FILE = Path(os.environ.get("IRIS_MEMORY_FILE", "iris-memory.json"))

mcp = FastMCP("iris-memory")


def _load() -> list[dict]:
    if not MEMORY_FILE.exists():
        return []
    try:
        data = json.loads(MEMORY_FILE.read_text("utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(items: list[dict]) -> None:
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=MEMORY_FILE.parent or ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(items, handle, indent=2, ensure_ascii=False)
    os.replace(tmp, MEMORY_FILE)


@mcp.tool()
def remember(text: str, tags: Optional[str] = None) -> str:
    """Save a durable note. Use for facts, preferences, and context worth keeping.

    Args:
        text: The note to remember.
        tags: Optional comma-separated tags to make recall easier.
    """
    items = _load()
    entry = {
        "id": (items[-1]["id"] + 1) if items else 1,
        "text": text.strip(),
        "tags": [t.strip() for t in (tags or "").split(",") if t.strip()],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    items.append(entry)
    _save(items)
    return f"Saved note #{entry['id']}."


@mcp.tool()
def recall(query: Optional[str] = None, limit: int = 20) -> str:
    """Recall saved notes, optionally filtered by a word or tag.

    Args:
        query: Optional text to match against note bodies and tags.
        limit: Maximum number of notes to return (most recent first).
    """
    items = _load()
    if query:
        needle = query.lower()
        items = [i for i in items if needle in i["text"].lower() or any(needle in t.lower() for t in i.get("tags", []))]
    if not items:
        return "No matching notes." if query else "No notes saved yet."
    items = items[-limit:][::-1]
    lines = []
    for i in items:
        tags = f"  [{', '.join(i['tags'])}]" if i.get("tags") else ""
        lines.append(f"#{i['id']} ({i['created_at']}){tags}: {i['text']}")
    return "\n".join(lines)


@mcp.tool()
def forget(note_id: int) -> str:
    """Delete a saved note by its id.

    Args:
        note_id: The id of the note to remove (from recall output).
    """
    items = _load()
    remaining = [i for i in items if i["id"] != note_id]
    if len(remaining) == len(items):
        return f"No note #{note_id}."
    _save(remaining)
    return f"Deleted note #{note_id}."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
