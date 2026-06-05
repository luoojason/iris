"""Tests for the Discord adapter's pure logic (reply chunking).

The Discord client itself needs discord.py and a live connection, so it is not
unit-tested here, but the message-splitting logic is pure and worth pinning.
"""

from __future__ import annotations

from iris.discord_adapter import _chunk, DISCORD_LIMIT


def test_empty_text_is_no_messages():
    assert _chunk("") == []


def test_short_text_is_one_message():
    assert _chunk("hello") == ["hello"]


def test_long_text_splits_under_limit():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    chunks = _chunk(text)
    assert len(chunks) > 1
    assert all(len(c) <= DISCORD_LIMIT for c in chunks)
    # nothing is dropped (ignoring the newlines consumed at split points)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_single_line_longer_than_limit_is_hard_split():
    text = "y" * (DISCORD_LIMIT * 2 + 10)
    chunks = _chunk(text)
    assert all(len(c) <= DISCORD_LIMIT for c in chunks)
    assert "".join(chunks) == text
