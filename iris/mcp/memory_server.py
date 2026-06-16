"""A tiny MCP memory server: give the agent durable notes across sessions.

This is an example of how Iris adds durable capabilities while the brain
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

import os
from pathlib import Path
from typing import Optional

import time as _time

from iris.memory import DEFAULT_IMPORTANCE, normalize, rank
from iris.statefile import JsonListStore

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


def _store() -> JsonListStore:
    # Built per call from the module global so tests that monkeypatch MEMORY_FILE
    # still hit the right path; a JsonListStore is a cheap stateless wrapper.
    return JsonListStore(MEMORY_FILE, "memory")


def _locked():
    """Hold an exclusive cross-process lock for a load-modify-save."""
    return _store().locked()


def _load() -> list[dict]:
    return _store().load()


def _save(items: list[dict]) -> None:
    _store().save(items)


def _fmt(entry: dict) -> str:
    """One display line for a note, surfacing the signals the model can act on."""
    note = normalize(entry)
    marks = []
    if note["pinned"]:
        marks.append("PINNED")
    if note["importance"] != DEFAULT_IMPORTANCE:
        marks.append(f"imp{note['importance']}")
    if note["use_count"]:
        marks.append(f"used{note['use_count']}x")
    meta = f" <{', '.join(marks)}>" if marks else ""
    tags = f"  [{', '.join(note['tags'])}]" if note["tags"] else ""
    return f"#{note['id']} ({note.get('created_at') or '?'}){meta}{tags}: {note['text']}"


@mcp.tool()
def remember(text: str, tags: Optional[str] = None, importance: int = DEFAULT_IMPORTANCE,
             pinned: bool = False) -> str:
    """Save a durable note. Use for facts, preferences, and context worth keeping.

    Args:
        text: The note to remember.
        tags: Optional comma-separated tags to make recall easier.
        importance: 1-5, how much this should outrank other notes (default 3).
        pinned: Pin a note so it always surfaces near the top of recall.
    """
    with _locked():
        items = _load()
        next_id = max((int(i.get("id", 0)) for i in items), default=0) + 1
        entry = {
            "id": next_id,
            "text": text.strip(),
            "tags": [t.strip() for t in (tags or "").split(",") if t.strip()],
            "created_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
            "importance": max(1, min(5, int(importance))),
            "pinned": bool(pinned),
            "use_count": 0,
            "last_used": None,
        }
        items.append(entry)
        _save(items)
    return f"Saved note #{next_id}."


@mcp.tool()
def recall(query: Optional[str] = None, limit: int = 20) -> str:
    """Recall saved notes, ranked by relevance, importance, recency, and pinning.

    Read-only: recall never changes a note's standing. When a recalled note
    actually informs your reply, call ``mark_useful`` on it so future recall
    learns to surface it sooner.

    Args:
        query: Optional words or tags to search for; omit to browse top notes.
        limit: Maximum number of notes to return.
    """
    items = _load()
    if not items:
        return "No notes saved yet."
    now = _time.time()
    ranked = rank(items, query, now, limit)
    if not ranked:
        return "No matching notes."
    return "\n".join(_fmt(i) for i in ranked)


@mcp.tool()
def mark_useful(note_id: int) -> str:
    """Record that a recalled note actually helped answer the current message.

    This is the one signal that lets memory improve itself: call it sparingly and
    deliberately, only when a note you recalled genuinely informed your reply. It
    nudges that note's rank up as a tie-breaker; it never overrides importance,
    pinning, or relevance.

    Args:
        note_id: The id of the note that helped (from recall output).
    """
    with _locked():
        items = _load()
        for entry in items:
            if entry.get("id") == note_id:
                entry["use_count"] = max(0, int(entry.get("use_count", 0))) + 1
                entry["last_used"] = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
                _save(items)
                return f"Noted: #{note_id} marked useful ({entry['use_count']}x)."
    return f"No note #{note_id}."


@mcp.tool()
def pin(note_id: int, pinned: bool = True) -> str:
    """Pin (or unpin) a note so it always surfaces near the top of recall.

    Args:
        note_id: The id of the note to pin or unpin.
        pinned: True to pin (default), False to unpin.
    """
    with _locked():
        items = _load()
        for entry in items:
            if entry.get("id") == note_id:
                entry["pinned"] = bool(pinned)
                _save(items)
                return f"{'Pinned' if pinned else 'Unpinned'} note #{note_id}."
    return f"No note #{note_id}."


@mcp.tool()
def set_importance(note_id: int, importance: int) -> str:
    """Re-weight an existing note's importance (1-5) as you learn what matters.

    Use this when a note turns out to matter more or less than when you saved it,
    without rewriting it: bump a standing preference up, drop stale trivia down.

    Args:
        note_id: The id of the note to re-weight.
        importance: New importance from 1 (trivial) to 5 (always surface).
    """
    value = max(1, min(5, int(importance)))
    with _locked():
        items = _load()
        for entry in items:
            if entry.get("id") == note_id:
                entry["importance"] = value
                _save(items)
                return f"Set note #{note_id} importance to {value}."
    return f"No note #{note_id}."


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
