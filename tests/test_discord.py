"""Tests for reply chunking and the should-handle gate.

The client itself needs the SDK and a live connection, but the message-splitting
logic and the should_handle decision are pure and worth pinning.
"""

from __future__ import annotations

from types import SimpleNamespace

from iris.discord_adapter import should_handle
from iris.textutil import chunk_text

DISCORD_LIMIT = 2000

BOT = SimpleNamespace(id=1)


def _cfg(allowed_channel_ids=(), allowed_user_ids=(), respond_without_mention=False):
    return SimpleNamespace(
        allowed_channel_ids=list(allowed_channel_ids),
        allowed_user_ids=list(allowed_user_ids),
        respond_without_mention=respond_without_mention,
    )


def _msg(channel_id=10, parent_id=None, guild=object(), author_id=2, is_bot=False, mentions=()):
    channel = SimpleNamespace(id=channel_id, parent_id=parent_id, guild=guild)
    author = SimpleNamespace(id=author_id, bot=is_bot)
    return SimpleNamespace(author=author, channel=channel, mentions=list(mentions))


def test_ignores_own_and_other_bots():
    assert should_handle(_msg(author_id=1), BOT, _cfg()) is False        # itself
    assert should_handle(_msg(is_bot=True), BOT, _cfg()) is False        # another bot


def test_respects_user_allowlist():
    cfg = _cfg(allowed_user_ids=["2"])
    assert should_handle(_msg(author_id=2, mentions=[BOT]), BOT, cfg) is True
    assert should_handle(_msg(author_id=999, mentions=[BOT]), BOT, cfg) is False


def test_channel_lock_blocks_other_channels():
    cfg = _cfg(allowed_channel_ids=["10"])
    assert should_handle(_msg(channel_id=10, mentions=[BOT]), BOT, cfg) is True
    assert should_handle(_msg(channel_id=77, mentions=[BOT]), BOT, cfg) is False


def test_general_channel_needs_mention_unless_configured():
    cfg = _cfg(allowed_channel_ids=["10"])
    assert should_handle(_msg(channel_id=10), BOT, cfg) is False           # no mention
    assert should_handle(_msg(channel_id=10, mentions=[BOT]), BOT, cfg) is True
    cfg_open = _cfg(allowed_channel_ids=["10"], respond_without_mention=True)
    assert should_handle(_msg(channel_id=10), BOT, cfg_open) is True


def test_thread_under_allowed_channel_auto_responds():
    cfg = _cfg(allowed_channel_ids=["10"])
    # A thread (parent_id=10) gets answered even with no mention and no
    # respond_without_mention, because threads are project spaces.
    thread_msg = _msg(channel_id=555, parent_id=10)
    assert should_handle(thread_msg, BOT, cfg) is True


def test_thread_under_other_channel_is_ignored():
    cfg = _cfg(allowed_channel_ids=["10"])
    thread_msg = _msg(channel_id=555, parent_id=88)  # parent not allowed
    assert should_handle(thread_msg, BOT, cfg) is False


def test_dm_is_always_handled():
    assert should_handle(_msg(guild=None), BOT, _cfg()) is True


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
