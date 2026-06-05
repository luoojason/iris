"""A tiny MCP memory server: give the agent durable notes across sessions.

This is an example of how Iris keeps Hermes-style capabilities while the brain
is the official ``claude`` binary. Instead of reimplementing a tool system, we
expose tools the way Claude Code already understands them: as an MCP server.
``claude --mcp-config`` launches this process and the model can call its tools.

Storage is a flat JSON file (``IRIS_MEMORY_FILE``, default ``iris-memory.json``)
so it is trivial to read and back up. Writes take a cross-process lock, and
reads tolerate hand-edited or legacy notes.

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
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

import time as _time

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

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


@contextmanager
def _locked():
    """Hold an exclusive cross-process lock for a load-modify-save."""
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        yield
        return
    lock_path = MEMORY_FILE.with_suffix(MEMORY_FILE.suffix + ".lock")
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


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
    with _locked():
        items = _load()
        next_id = max((int(i.get("id", 0)) for i in items), default=0) + 1
        entry = {
            "id": next_id,
            "text": text.strip(),
            "tags": [t.strip() for t in (tags or "").split(",") if t.strip()],
            "created_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
        }
        items.append(entry)
        _save(items)
    return f"Saved note #{next_id}."


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
        items = [
            i for i in items
            if needle in str(i.get("text", "")).lower()
            or any(needle in str(t).lower() for t in i.get("tags", []))
        ]
    if not items:
        return "No matching notes." if query else "No notes saved yet."
    items = items[-limit:][::-1]
    lines = []
    for i in items:
        tags = f"  [{', '.join(i.get('tags', []))}]" if i.get("tags") else ""
        lines.append(f"#{i.get('id', '?')} ({i.get('created_at', '?')}){tags}: {i.get('text', '')}")
    return "\n".join(lines)


@mcp.tool()
def forget(note_id: int) -> str:
    """Delete a saved note by its id.

    Args:
        note_id: The id of the note to remove (from recall output).
    """
    with _locked():
        items = _load()
        remaining = [i for i in items if i.get("id") != note_id]
        if len(remaining) == len(items):
            return f"No note #{note_id}."
        _save(remaining)
    return f"Deleted note #{note_id}."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
