"""Tests for reminder time parsing and the store's recurrence handling."""

from __future__ import annotations

import pytest

from iris.reminders import ReminderStore, parse_every, parse_when


def test_parse_when_relative_and_iso():
    assert parse_when("+30m", now=0) == 1800
    assert parse_when("+2h", now=0) == 7200
    assert parse_when("+1d", now=0) == 86400
    assert parse_when("2026-06-07T00:00:00Z") > 0


def test_parse_every_forms():
    assert parse_every("every 30m") == 1800
    assert parse_every("2h") == 7200  # 'every' is optional sugar
    assert parse_every("1d") == 86400
    assert parse_every("") == 0  # empty means one-shot


def test_parse_every_rejects_garbage():
    with pytest.raises(ValueError):
        parse_every("sometimes")
    with pytest.raises(ValueError):
        parse_every("every 5x")


def test_one_shot_pops_once_and_is_gone(tmp_path):
    store = ReminderStore(tmp_path / "r.json")
    store.add(due_ts=100, text="ping", channel_id="c1")
    assert [j["text"] for j in store.pop_due(now=200)] == ["ping"]
    assert store.pop_due(now=300) == []  # not rescheduled
    assert store.all() == []


def test_recurring_reschedules_from_now(tmp_path):
    store = ReminderStore(tmp_path / "r.json")
    store.add(due_ts=100, text="standup", channel_id="c1", repeat_secs=3600)
    fired = store.pop_due(now=150)
    assert [j["text"] for j in fired] == ["standup"]
    # still scheduled, next fire one period from *now*, not from the old due_ts
    remaining = store.all()
    assert len(remaining) == 1
    assert remaining[0]["due_ts"] == 150 + 3600


def test_missed_window_fires_once_not_every_occurrence(tmp_path):
    # Host asleep for a long time: a daily job that was due ages ago should fire
    # exactly once on the next tick, then resume cadence, not replay every day.
    store = ReminderStore(tmp_path / "r.json")
    store.add(due_ts=0, text="daily", channel_id="c1", repeat_secs=86400)
    fired = store.pop_due(now=10 * 86400)  # ten days late
    assert len(fired) == 1
    remaining = store.all()
    assert len(remaining) == 1
    assert remaining[0]["due_ts"] == 10 * 86400 + 86400


def test_recurring_preserves_id_and_payload(tmp_path):
    store = ReminderStore(tmp_path / "r.json")
    rid = store.add(due_ts=100, text="water", channel_id="c9", repeat_secs=60)
    store.pop_due(now=100)
    nxt = store.all()[0]
    assert nxt["id"] == rid
    assert nxt["channel_id"] == "c9"
    assert nxt["repeat_secs"] == 60
