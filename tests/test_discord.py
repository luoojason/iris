"""Tests for reply chunking, shared by the Discord and Telegram adapters.

The clients themselves need their SDKs and a live connection, so they are not
unit-tested here, but the message-splitting logic is pure and worth pinning.
"""

from __future__ import annotations

from iris.textutil import chunk_text

DISCORD_LIMIT = 2000


def test_empty_text_is_no_messages():
    assert chunk_text("", DISCORD_LIMIT) == []


def test_short_text_is_one_message():
    assert chunk_text("hello", DISCORD_LIMIT) == ["hello"]


def test_long_text_splits_under_limit():
    text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    chunks = chunk_text(text, DISCORD_LIMIT)
    assert len(chunks) > 1
    assert all(len(c) <= DISCORD_LIMIT for c in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_single_line_longer_than_limit_is_hard_split():
    text = "y" * (DISCORD_LIMIT * 2 + 10)
    chunks = chunk_text(text, DISCORD_LIMIT)
    assert all(len(c) <= DISCORD_LIMIT for c in chunks)
    assert "".join(chunks) == text


def test_respects_a_smaller_limit():
    text = "abcdefghij" * 10  # 100 chars
    chunks = chunk_text(text, 30)
    assert all(len(c) <= 30 for c in chunks)
    assert "".join(chunks) == text
