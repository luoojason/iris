"""Tests for the speak MCP tool (synthesis and HTTP upload both mocked)."""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from iris.mcp import tts_server as ts


def test_speak_synthesizes_and_posts(tmp_path, monkeypatch):
    posted = {}

    def fake_synth(text, out_path, *a, **k):
        with open(out_path, "w") as handle:
            handle.write("AUDIO")
        return out_path

    def fake_post(channel_id, file_path, content=""):
        posted["channel"] = channel_id
        posted["bytes"] = open(file_path, "rb").read()
        return {"ok": True, "status": 200}

    monkeypatch.setattr(ts, "synthesize", fake_synth)
    monkeypatch.setattr(ts, "_post_audio", fake_post)
    monkeypatch.setenv("IRIS_DISCORD_HOME_CHANNEL", "555")

    out = ts.speak("hello there")
    assert "Spoke the reply" in out
    assert posted["channel"] == "555"
    assert posted["bytes"] == b"AUDIO"


def test_speak_without_channel_is_friendly(monkeypatch):
    monkeypatch.delenv("IRIS_DISCORD_HOME_CHANNEL", raising=False)
    assert "home channel" in ts.speak("hi").lower()


def test_speak_surfaces_unavailable_tts(monkeypatch):
    def boom(text, out_path, *a, **k):
        raise ts.TTSUnavailable("no engine")

    monkeypatch.setattr(ts, "synthesize", boom)
    monkeypatch.setenv("IRIS_DISCORD_HOME_CHANNEL", "555")
    assert "Could not speak" in ts.speak("hi")


def test_speak_surfaces_post_error(tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "synthesize", lambda t, o, *a, **k: (open(o, "w").write("x"), o)[1])
    monkeypatch.setattr(ts, "_post_audio", lambda c, f, content="": {"error": "HTTP 403"})
    monkeypatch.setenv("IRIS_DISCORD_HOME_CHANNEL", "555")
    assert "403" in ts.speak("hi")


def test_multipart_has_payload_and_file_parts():
    content_type, body = ts._multipart("hi", "speech.wav", b"\x00\x01AUDIO")
    assert content_type.startswith("multipart/form-data; boundary=")
    assert b'name="payload_json"' in body
    assert b'name="files[0]"; filename="speech.wav"' in body
    assert b"\x00\x01AUDIO" in body
    assert b'"attachments"' in body
