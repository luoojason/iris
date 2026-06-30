"""Tests for the reminders MCP server's tools against a temp store file."""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")  # the server needs the MCP SDK; skip if absent

from iris.mcp import reminders as srv
from iris.reminders import ReminderStore


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "STORE", ReminderStore(tmp_path / "r.json"))
    monkeypatch.setattr(srv, "DEFAULT_CHANNEL", "home-1")
    return srv


def test_schedule_records_model_origin(server):
    out = server.schedule_reminder("stand up", "+30m")
    assert "#1" in out
    assert server.STORE.all()[0]["origin"] == "model"


def test_schedule_followup_kind(server):
    out = server.schedule_reminder("check the deploy", "+30m", kind="followup")
    assert "#1" in out
    item = server.STORE.all()[0]
    assert item["kind"] == "followup"


def test_schedule_rejects_unknown_kind(server):
    out = server.schedule_reminder("x", "+30m", kind="party")
    assert "kind" in out.lower()
    assert server.STORE.all() == []


def test_pending_cap_blocks_runaway_scheduling(server, monkeypatch):
    monkeypatch.setattr(srv, "MAX_PENDING", 3)
    for i in range(3):
        server.schedule_reminder(f"r{i}", "+30m")
    out = server.schedule_reminder("one too many", "+30m")
    assert "cancel" in out.lower()
    assert len(server.STORE.all()) == 3


def test_list_tags_followups(server):
    server.schedule_reminder("the deploy", "+30m", kind="followup")
    assert "followup" in server.list_reminders()


def test_pending_cap_reads_the_env_lazily(server, monkeypatch):
    # The server runs inside the claude child, which strips IRIS_* from its
    # env at spawn; the knob must be read at call time (after load_dotenv),
    # not at import time, or .env settings can never reach it.
    monkeypatch.setattr(srv, "MAX_PENDING", None)
    monkeypatch.setenv("IRIS_REMINDERS_MAX_PENDING", "1")
    srv.schedule_reminder("first", "+30m")
    out = srv.schedule_reminder("second", "+30m")
    assert "Cancel some" in out
    assert len(srv.STORE.all()) == 1


def test_max_pending_survives_a_non_numeric_env(monkeypatch):
    monkeypatch.setattr(srv, "MAX_PENDING", None)
    monkeypatch.setenv("IRIS_REMINDERS_MAX_PENDING", "lots")  # garbage
    assert srv._max_pending() == 25  # falls back to the default, does not raise


def test_default_channel_prefers_the_origin_thread(tmp_path, monkeypatch):
    # A reminder/follow-up set mid-conversation defaults to THAT thread, not the
    # home channel, so it comes back where the owner is actually talking.
    monkeypatch.setattr(srv, "STORE", ReminderStore(tmp_path / "r.json"))
    monkeypatch.setattr(srv, "DEFAULT_CHANNEL", None)
    monkeypatch.setenv("IRIS_ORIGIN_CHANNEL", "thread-9")
    monkeypatch.setenv("IRIS_DISCORD_HOME_CHANNEL", "home-1")
    srv.schedule_reminder("check the deploy", "+30m", kind="followup")
    assert srv.STORE.all()[0]["channel_id"] == "thread-9"


def test_default_channel_falls_back_to_home_without_origin(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "STORE", ReminderStore(tmp_path / "r.json"))
    monkeypatch.setattr(srv, "DEFAULT_CHANNEL", None)
    monkeypatch.delenv("IRIS_ORIGIN_CHANNEL", raising=False)
    monkeypatch.setenv("IRIS_DISCORD_HOME_CHANNEL", "home-1")
    srv.schedule_reminder("stand up", "+30m")
    assert srv.STORE.all()[0]["channel_id"] == "home-1"
