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


def test_finite_recurring_reminder_stops_after_its_remaining_fires(tmp_path):
    store = ReminderStore(tmp_path / "r.json")
    store.add(due_ts=100, text="standup", channel_id="c1", repeat_secs=60, remaining=2)
    # fire 1: spends one, reschedules with remaining 1
    assert [j["text"] for j in store.pop_due(now=100)] == ["standup"]
    rem = store.all()
    assert len(rem) == 1 and rem[0]["remaining"] == 1 and rem[0]["due_ts"] == 160
    # fire 2: last one, so it is dropped instead of rescheduled
    assert [j["text"] for j in store.pop_due(now=160)] == ["standup"]
    assert store.all() == []


def test_cron_reminder_recurs_instead_of_firing_once(tmp_path, monkeypatch):
    # Regression: a `cron:` reminder must recompute its next fire from the spec
    # and survive, not fire once and vanish.
    monkeypatch.setenv("IRIS_TZ", "UTC")
    from iris.reminders import cron_spec, parse_when
    store = ReminderStore(tmp_path / "r.json")
    when = "cron: 0 9 * * *"  # every day at 09:00 UTC
    base = 1780000000.0
    due = parse_when(when, now=base)
    store.add(due, "standup", "c1", cron=cron_spec(when))
    fired = store.pop_due(now=due + 1)
    assert [j["text"] for j in fired] == ["standup"]
    remaining = store.all()
    assert len(remaining) == 1                     # survived (did not vanish)
    assert remaining[0]["cron"] == "0 9 * * *"
    assert remaining[0]["due_ts"] > due            # advanced to the next 09:00


def test_infinite_recurring_reminder_unaffected_by_remaining(tmp_path):
    store = ReminderStore(tmp_path / "r.json")
    store.add(due_ts=100, text="forever", channel_id="c1", repeat_secs=60)  # no remaining
    store.pop_due(now=100)
    assert len(store.all()) == 1  # still scheduled


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


# -- follow-up kinds ----------------------------------------------------------


def test_add_stores_kind_and_origin(tmp_path):
    store = ReminderStore(tmp_path / "r.json")
    store.add(100.0, "check the deploy", "c1", kind="followup", origin="model")
    item = store.all()[0]
    assert item["kind"] == "followup"
    assert item["origin"] == "model"


def test_plain_add_keeps_the_old_record_shape(tmp_path):
    store = ReminderStore(tmp_path / "r.json")
    store.add(100.0, "stand up", "c1")
    item = store.all()[0]
    assert "kind" not in item and "origin" not in item


def test_render_reminder_distinguishes_followups():
    from iris.reminders import render_reminder

    plain = render_reminder({"text": "stand up"})
    promised = render_reminder({"text": "the deploy", "kind": "followup"})
    assert plain == "Reminder: stand up"
    assert promised.startswith("Follow-up")
    assert "the deploy" in promised


def test_requeue_preserves_identity_as_one_shot(tmp_path):
    store = ReminderStore(tmp_path / "r.json")
    store.add(100.0, "check it", "c1", repeat_secs=60, kind="followup", origin="model")
    due = store.pop_due(now=150.0)  # the recurrence reschedules itself in place
    store.requeue(due[0])
    items = store.all()
    requeued = [i for i in items if i.get("due_ts") == 100.0]
    assert len(requeued) == 1
    assert requeued[0]["kind"] == "followup"
    assert requeued[0]["origin"] == "model"
    # the firing goes back one-shot; its next occurrence is already queued
    assert int(requeued[0].get("repeat_secs", 0) or 0) == 0


def test_corrupt_reminders_file_is_quarantined(tmp_path):
    # Migration to the shared store fixed a drift: a corrupt file used to be
    # silently discarded (next save overwrote it). Now it is preserved.
    p = tmp_path / "r.json"
    p.write_text("{not json", encoding="utf-8")
    store = ReminderStore(p)
    assert store.all() == []
    assert (tmp_path / "r.json.corrupt").exists()  # owner data kept, not overwritten
