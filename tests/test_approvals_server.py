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
    monkeypatch.delenv("IRIS_ORIGIN_CHANNEL", raising=False)
    assert srv._post_approval("r1", "do thing", Config()) is False  # no channel/token


def _capture_post_channel(monkeypatch):
    import urllib.request
    captured = {}

    class FakeResp:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=15):
        captured["url"] = req.full_url
        return FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return captured


def test_post_approval_prefers_the_origin_thread(monkeypatch):
    # An Approve/Deny prompt raised during a thread turn appears in THAT thread,
    # not the home channel, so the owner approves where they are working.
    from iris.config import Config
    captured = _capture_post_channel(monkeypatch)
    monkeypatch.setenv("IRIS_ORIGIN_CHANNEL", "thread-9")
    assert srv._post_approval("r1", "do thing", Config(home_channel="home-1", discord_token="tok")) is True
    assert "/channels/thread-9/messages" in captured["url"]


def test_post_approval_falls_back_to_home_without_origin(monkeypatch):
    from iris.config import Config
    captured = _capture_post_channel(monkeypatch)
    monkeypatch.delenv("IRIS_ORIGIN_CHANNEL", raising=False)
    assert srv._post_approval("r1", "do thing", Config(home_channel="home-1", discord_token="tok")) is True
    assert "/channels/home-1/messages" in captured["url"]
