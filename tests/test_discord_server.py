"""Tests for the scoped Discord-actions MCP tools (HTTP layer mocked)."""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from iris.mcp import discord_server as ds


def test_create_thread_uses_home_channel(monkeypatch):
    calls = []

    def fake(method, path, body=None):
        calls.append((method, path, body))
        return {"name": body["name"], "id": "999"}

    monkeypatch.setattr(ds, "discord_request", fake)
    monkeypatch.setenv("IRIS_DISCORD_HOME_CHANNEL", "123")
    out = ds.create_thread("plan the trip")
    assert "999" in out and "plan the trip" in out
    assert calls[0][0] == "POST"
    assert "/channels/123/threads" in calls[0][1]
    assert calls[0][2]["type"] == 11


def test_create_thread_without_channel_is_friendly(monkeypatch):
    monkeypatch.delenv("IRIS_DISCORD_HOME_CHANNEL", raising=False)
    assert "home channel" in ds.create_thread("x").lower()


def test_fetch_messages_formats(monkeypatch):
    monkeypatch.setattr(ds, "discord_request",
                        lambda m, p, b=None: [{"author": {"username": "jay"}, "content": "hi there"}])
    monkeypatch.setenv("IRIS_DISCORD_HOME_CHANNEL", "123")
    assert "jay: hi there" in ds.fetch_messages()


def test_error_is_surfaced_not_raised(monkeypatch):
    monkeypatch.setattr(ds, "discord_request", lambda m, p, b=None: {"error": "HTTP 403"})
    monkeypatch.setenv("IRIS_DISCORD_HOME_CHANNEL", "123")
    assert "403" in ds.fetch_messages()


def test_list_channels_filters_to_text(monkeypatch):
    monkeypatch.delenv("IRIS_DISCORD_ALLOWED_GUILDS", raising=False)
    monkeypatch.setenv("IRIS_DISCORD_GUILD_ID", "guild1")  # default-deny: name the guild
    monkeypatch.setattr(ds, "discord_request", lambda m, p, b=None: [
        {"name": "general", "id": "1", "type": 0},
        {"name": "Voice", "id": "2", "type": 2},  # voice, should be dropped
    ])
    out = ds.list_channels("guild1")
    assert "#general" in out and "Voice" not in out


def test_channel_allowlist_blocks_other_channels(monkeypatch):
    monkeypatch.setenv("IRIS_DISCORD_ALLOWED_CHANNELS", "123")
    out = ds.fetch_messages(channel_id="999")  # not in the allowlist
    assert "IRIS_DISCORD_ALLOWED_CHANNELS" in out


def test_channel_allowlist_admits_listed_channel(monkeypatch):
    monkeypatch.setenv("IRIS_DISCORD_ALLOWED_CHANNELS", "123")
    monkeypatch.setattr(ds, "discord_request",
                        lambda m, p, b=None: [{"author": {"username": "jay"}, "content": "hi"}])
    assert "jay: hi" in ds.fetch_messages(channel_id="123")


def test_guild_allowlist_blocks_other_guilds(monkeypatch):
    monkeypatch.setenv("IRIS_DISCORD_ALLOWED_GUILDS", "g1")
    out = ds.list_channels("g2")
    assert "IRIS_DISCORD_ALLOWED_GUILDS" in out


def test_fetch_messages_default_denies_a_non_home_channel(monkeypatch):
    # Default-deny: with a home channel but no explicit allowlist, an arbitrary
    # channel must NOT be reachable (it would be a data-exfil primitive).
    monkeypatch.setenv("IRIS_DISCORD_HOME_CHANNEL", "123")
    monkeypatch.delenv("IRIS_DISCORD_ALLOWED_CHANNELS", raising=False)

    def must_not_call(*a, **k):
        raise AssertionError("must not reach the Discord API for a non-home channel")

    monkeypatch.setattr(ds, "discord_request", must_not_call)
    out = ds.fetch_messages(channel_id="999")
    assert "IRIS_DISCORD_ALLOWED_CHANNELS" in out


def test_list_channels_default_denies_a_non_configured_guild(monkeypatch):
    monkeypatch.delenv("IRIS_DISCORD_GUILD_ID", raising=False)
    monkeypatch.delenv("IRIS_DISCORD_ALLOWED_GUILDS", raising=False)

    def must_not_call(*a, **k):
        raise AssertionError("must not reach the Discord API for a non-configured guild")

    monkeypatch.setattr(ds, "discord_request", must_not_call)
    out = ds.list_channels("g2")
    assert "IRIS_DISCORD_ALLOWED_GUILDS" in out


def test_home_channel_is_allowed_without_an_explicit_allowlist(monkeypatch):
    monkeypatch.setenv("IRIS_DISCORD_HOME_CHANNEL", "123")
    monkeypatch.delenv("IRIS_DISCORD_ALLOWED_CHANNELS", raising=False)
    monkeypatch.setattr(ds, "discord_request",
                        lambda m, p, b=None: [{"author": {"username": "jay"}, "content": "hi"}])
    assert "jay: hi" in ds.fetch_messages(channel_id="123")  # home is allowed by default


def test_configured_guild_is_allowed_without_an_explicit_allowlist(monkeypatch):
    monkeypatch.setenv("IRIS_DISCORD_GUILD_ID", "g1")
    monkeypatch.delenv("IRIS_DISCORD_ALLOWED_GUILDS", raising=False)
    monkeypatch.setattr(ds, "discord_request",
                        lambda m, p, b=None: [{"name": "general", "id": "1", "type": 0}])
    assert "#general" in ds.list_channels()  # the configured guild is allowed by default
