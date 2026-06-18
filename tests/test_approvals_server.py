"""Tests for the approvals MCP server glue (no real Discord/claude)."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("mcp")

from iris.mcp import approvals_server as srv


def test_check_auto_allows_a_safe_tool(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no repo .env in scope
    monkeypatch.setenv("IRIS_APPROVALS_FILE", str(tmp_path / "a.json"))
    out = srv.check("mcp__memory__recall", {"query": "x"})
    assert json.loads(out) == {"behavior": "allow"}  # safe -> no prompt, no Discord


def test_check_fails_closed_when_owner_unreachable(tmp_path, monkeypatch):
    # A risky tool, but no channel/token configured -> can't post -> deny (no poll/hang).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IRIS_APPROVALS_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("IRIS_DISCORD_TOKEN", "")
    monkeypatch.setenv("IRIS_DISCORD_HOME_CHANNEL", "")
    out = srv.check("mcp__publish__publish_video", {"mp4_path": "/x.mp4"})
    assert json.loads(out)["behavior"] == "deny"


def test_post_approval_false_without_channel(monkeypatch):
    from iris.config import Config
    assert srv._post_approval("r1", "do thing", Config()) is False  # no channel/token
