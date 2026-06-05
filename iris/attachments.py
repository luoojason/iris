"""Handle inbound files and images across transports.

The brain is the official ``claude`` binary, whose native ``Read`` tool ingests
images and files. So to let the agent see an attachment we just download it to a
per-conversation directory the agent is allowed to read, and mention its path in
the prompt. Each transport downloads in its own way; the path/prompt shaping is
shared here.
"""

from __future__ import annotations

import re
from pathlib import Path

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def conversation_dir(base: str, conversation_id: str) -> Path:
    """A per-conversation directory under ``base`` for this chat's files."""
    safe = _SAFE.sub("_", conversation_id) or "conversation"
    directory = Path(base).expanduser() / safe
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def safe_filename(name: str | None) -> str:
    """A filesystem-safe leaf name (no directory traversal)."""
    leaf = Path(name or "file").name
    return _SAFE.sub("_", leaf) or "file"


def describe(text: str, paths: list[str]) -> str:
    """Fold attachment paths into the prompt so the brain knows to read them."""
    if not paths:
        return text
    lines = [text] if text else []
    if not text:
        lines.append("(no text, see the attached file(s))")
    for path in paths:
        lines.append(f"[attached file: {path}]")
    return "\n".join(lines).strip()
