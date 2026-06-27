"""Tests for the morning briefing (iris/briefing.py): a model-free, network-free
status read over Iris's autonomy state. Everything runs on tmp_path stores."""

from __future__ import annotations

from iris.approvals import ApprovalStore
from iris.briefing import build_briefing
from iris.config import Config
from iris.goals import GoalStore
from iris.inbox import Inbox
from iris.reminders import ReminderStore
from iris.schedules import ScheduleStore, add_rule

NOW = 1_780_000_000.0  # a fixed instant so the day window is deterministic
DAY = 86400.0


def _config(tmp_path, **overrides):
    fields = dict(
        goals_file=str(tmp_path / "goals.json"),
        schedules_file=str(tmp_path / "schedules.json"),
        heartbeat_file=str(tmp_path / "heartbeat.json"),  # absent unless written
        inbox_file=str(tmp_path / "inbox.json"),
        approvals_file=str(tmp_path / "approvals.json"),
    )
    fields.update(overrides)
    return Config(**fields)


def test_build_briefing_aggregates_every_section(tmp_path, monkeypatch):
    reminders_path = tmp_path / "reminders.json"
    monkeypatch.setenv("IRIS_REMINDERS_FILE", str(reminders_path))

    # Two reminders: one due now (today), one five days out (excluded).
    reminders = ReminderStore(reminders_path)
    reminders.add(NOW, "call the dentist", channel_id="c1")
    reminders.add(NOW + 5 * DAY, "next week thing", channel_id="c1")

    # An active goal at 0/5 steps.
    GoalStore(tmp_path / "goals.json").add("ship the thing", max_steps=5, now=NOW)

    # A schedule rule that fires daily.
    schedules = ScheduleStore(tmp_path / "schedules.json")
    add_rule(schedules, title="daily digest", when="2026-12-01T09:00:00Z",
             every="every 1d", instructions="post the digest", default_cap=62, now=NOW)

    # A heartbeat check that fails deterministically (a file that isn't there).
    (tmp_path / "heartbeat.json").write_text(
        '[{"name": "backup", "kind": "file_fresh", '
        '"path": "/no/such/iris/file", "max_age_secs": 100}]', "utf-8")

    # A pending inbox note and an undecided approval.
    Inbox(tmp_path / "inbox.json").append("a finished job left a note")
    ApprovalStore(tmp_path / "approvals.json").create("req-1", "risky tool", now=NOW)

    text = build_briefing(_config(tmp_path), now=NOW)

    assert "Morning briefing" in text
    assert "call the dentist" in text          # reminder due today
    assert "next week thing" not in text       # the far-future one is excluded
    assert "Reminders due by end of today: 1" in text
    assert "ship the thing" in text and "0/5" in text   # the goal at N/max
    assert "daily digest" in text              # the schedule's next fire
    assert "Fired this month: 0" in text
    assert "backup" in text                    # the failing heartbeat check
    assert "Inbox: 1 pending note(s)" in text
    assert "Approvals: 1 awaiting your tap" in text


def test_build_briefing_quiet_when_everything_empty(tmp_path, monkeypatch):
    # A reminders file that does not exist, so nothing is due.
    monkeypatch.setenv("IRIS_REMINDERS_FILE", str(tmp_path / "no-reminders.json"))
    text = build_briefing(_config(tmp_path), now=NOW)
    assert text == "All quiet: nothing due, no active goals, and nothing waiting on you."


def test_build_briefing_omits_empty_sections(tmp_path, monkeypatch):
    # Only an active goal exists; no reminders, schedules, heartbeat, or waiting work.
    monkeypatch.setenv("IRIS_REMINDERS_FILE", str(tmp_path / "no-reminders.json"))
    GoalStore(tmp_path / "goals.json").add("write the spec", max_steps=10, now=NOW)
    text = build_briefing(_config(tmp_path), now=NOW)
    assert "write the spec" in text
    assert "Reminders" not in text
    assert "Next schedule" not in text
    assert "Heartbeat" not in text
    assert "Inbox" not in text
