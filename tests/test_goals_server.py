"""Tests for the goals MCP server's tools against a temp store file."""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")  # the server needs the MCP SDK; skip if absent

from iris.goals import GoalStore
from iris.mcp import goals as srv


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "STORE", GoalStore(tmp_path / "g.json"))
    monkeypatch.setattr(srv, "DEFAULT_CHANNEL", "home-1")
    monkeypatch.setattr(srv, "MAX_ACTIVE", 10)
    monkeypatch.delenv("IRIS_ORIGIN_CHANNEL", raising=False)
    return srv


def test_set_goal_records_active_scoped_to_origin_thread(server, monkeypatch):
    monkeypatch.setenv("IRIS_ORIGIN_CHANNEL", "chan-9")
    out = server.set_goal("ship the roadmap")
    assert "#1" in out
    goal = server.STORE.all()[0]
    assert goal["status"] == "active"
    assert goal["text"] == "ship the roadmap"
    # routes reports back to the thread it was set in
    assert goal["conversation_id"] == "discord:chan-9"


def test_set_goal_falls_back_to_home_channel(server):
    server.set_goal("a goal with no thread")
    assert server.STORE.all()[0]["conversation_id"] == "discord:home-1"


def test_set_goal_honors_a_custom_step_budget(server):
    server.set_goal("a long goal", max_steps=50)
    assert server.STORE.all()[0]["max_steps"] == 50


def test_list_goals_shows_active_goals(server):
    server.set_goal("first goal")
    out = server.list_goals()
    assert "first goal" in out and "#1" in out


def test_list_goals_when_empty(server):
    assert "no" in server.list_goals().lower()


def test_cancel_goal_transitions_to_cancelled(server):
    server.set_goal("never mind this")
    out = server.cancel_goal(1)
    assert "1" in out
    assert server.STORE.get(1)["status"] == "cancelled"
    assert server.STORE.active() == []


def test_cancel_unknown_goal(server):
    assert "no" in server.cancel_goal(99).lower()


def test_active_cap_blocks_runaway_goal_setting(server, monkeypatch):
    monkeypatch.setattr(srv, "MAX_ACTIVE", 2)
    server.set_goal("g1")
    server.set_goal("g2")
    out = server.set_goal("g3 too many")
    assert "cancel" in out.lower()
    assert len(server.STORE.active()) == 2


def test_max_active_reads_env_lazily(server, monkeypatch):
    monkeypatch.setattr(srv, "MAX_ACTIVE", None)
    monkeypatch.setenv("IRIS_GOALS_MAX_ACTIVE", "1")
    server.set_goal("first")
    out = server.set_goal("second")
    assert "cancel" in out.lower()
    assert len(server.STORE.active()) == 1
