"""Shared helpers for splitting replies to per-platform message limits."""

from __future__ import annotations

FENCE = "```"


def chunk_text(text: str, limit: int) -> list[str]:
    """Split text into pieces no longer than ``limit``, preferring line breaks.

    Returns an empty list for empty text. Lines longer than ``limit`` are hard
    split. No content is dropped (aside from newlines consumed at split points).

    Code fences are kept balanced: when a split lands inside a ``` block, the
    piece is closed with a fence and the next piece reopens it (carrying the
    language), so a code-heavy reply renders correctly on each message instead
    of leaving a dangling fence. A little headroom is reserved for those
    wrapper fences.
    """
    text = text or ""
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    # Leave room so reopening/closing a split fence cannot push a piece over the
    # platform limit (covers a long opening ```language line plus a closer).
    inner = max(16, limit - 128)
    return _balance_fences(_split_by_lines(text, inner))


def _split_by_lines(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks


def _balance_fences(chunks: list[str]) -> list[str]:
    """Wrap any chunk boundary that falls inside a ``` block so each piece is balanced."""
    out: list[str] = []
    in_fence = False
    opener = FENCE  # the opening line, e.g. "```python\n", carried across a split
    for chunk in chunks:
        prefix = ""
        if in_fence:
            prefix = opener if opener.endswith("\n") else opener + "\n"
        for line in chunk.splitlines(keepends=True):
            if line.strip().startswith(FENCE):
                if not in_fence:
                    in_fence = True
                    opener = line if line.endswith("\n") else line + "\n"
                else:
                    in_fence = False
                    opener = FENCE
        piece = prefix + chunk
        if in_fence:
            if not piece.endswith("\n"):
                piece += "\n"
            piece += FENCE
        out.append(piece)
    return out
