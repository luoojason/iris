"""Telegram adapter tests: the single-user gate, exercised in isolation.

The gate that keeps the bot answering only its owner is compliance-critical, so
it is pulled out as a pure function and pinned here, mirroring the Discord twin.
"""

from __future__ import annotations

from types import SimpleNamespace

from iris.config import Config
from iris.telegram_adapter import is_allowed_update


def _update(user_id):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id))


def test_empty_allowlist_allows_anyone():
    cfg = Config()  # allowed_user_ids defaults to empty
    assert is_allowed_update(_update(123), cfg) is True


def test_allowlist_blocks_strangers_and_admits_the_owner():
    cfg = Config(allowed_user_ids=["111"])
    assert is_allowed_update(_update(111), cfg) is True   # int id compared as str
    assert is_allowed_update(_update(999), cfg) is False


def test_update_without_a_user_is_rejected():
    cfg = Config(allowed_user_ids=["111"])
    assert is_allowed_update(SimpleNamespace(effective_user=None), cfg) is False
