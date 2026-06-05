"""Shared helpers for splitting replies to per-platform message limits."""

from __future__ import annotations


def chunk_text(text: str, limit: int) -> list[str]:
    """Split text into pieces no longer than ``limit``, preferring line breaks.

    Returns an empty list for empty text. Lines longer than ``limit`` are hard
    split. No content is dropped (aside from newlines consumed at split points).
    """
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
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
