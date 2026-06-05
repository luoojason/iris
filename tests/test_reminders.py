"""Tests for reminder scheduling, storage, and delivery."""

from __future__ import annotations

import pytest

import iris.reminders as rem
from iris.cli import reminders_tick
from iris.config import Config
from iris.reminders import ReminderStore, parse_when


def test_parse_relative_offsets():
    now = 1000.0
    assert parse_when("+30m", now) == 1000 + 1800
    assert parse_when("+2h", now) == 1000 + 7200
    assert parse_when("+1d", now) == 1000 + 86400


def test_parse_iso_and_bad():
    assert parse_when("2026-06-06T00:00:00+00:00") > 0
    with pytest.raises(ValueError):
        parse_when("whenever")


def test_store_add_all_remove(tmp_path):
    s = ReminderStore(tmp_path / "r.json")
    rid = s.add(2000.0, "drink water", "chan1")
    assert rid == 1
    assert len(s.all()) == 1
    assert s.remove(1) is True
    assert s.remove(1) is False


def test_ids_do_not_collide_among_pending(tmp_path):
    s = ReminderStore(tmp_path / "r.json")
    a = s.add(1.0, "a", "c")
    b = s.add(2.0, "b", "c")
    assert a != b
    s.remove(a)
    c = s.add(3.0, "c", "c")
    assert c != b  # the still-pending reminder keeps a distinct id


def test_pop_due_only_takes_past(tmp_path):
    s = ReminderStore(tmp_path / "r.json")
    s.add(100.0, "past", "c")
    s.add(10000.0, "future", "c")
    due = s.pop_due(now=200.0)
    assert len(due) == 1 and due[0]["text"] == "past"
    assert [i["text"] for i in s.all()] == ["future"]


def test_reminders_tick_delivers_and_clears(tmp_path, monkeypatch):
    rfile = tmp_path / "r.json"
    monkeypatch.setenv("IRIS_REMINDERS_FILE", str(rfile))
    ReminderStore(rfile).add(1.0, "ping", "chan1")
    calls = []
    monkeypatch.setattr(rem, "send_discord_message", lambda ch, content, token: calls.append((ch, content)) or True)
    assert reminders_tick(Config(discord_token="tok")) == 0
    assert calls and calls[0][0] == "chan1" and "ping" in calls[0][1]
    assert ReminderStore(rfile).all() == []


def test_reminders_tick_requeues_on_send_failure(tmp_path, monkeypatch):
    rfile = tmp_path / "r.json"
    monkeypatch.setenv("IRIS_REMINDERS_FILE", str(rfile))
    ReminderStore(rfile).add(1.0, "ping", "chan1")
    monkeypatch.setattr(rem, "send_discord_message", lambda ch, content, token: False)
    reminders_tick(Config(discord_token="tok"))
    assert len(ReminderStore(rfile).all()) == 1  # not lost
