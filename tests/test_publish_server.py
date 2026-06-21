"""Tests for the publish MCP tool (the underlying Buffer client is faked)."""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from iris.mcp import publish_server as ps


def test_publish_tool_missing_file():
    assert "No such file" in ps.publish_video("/nope/x.mp4", "cap")


def test_publish_tool_formats_results(tmp_path, monkeypatch):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    monkeypatch.delenv("IRIS_PUBLISH_DIR", raising=False)
    monkeypatch.setattr(ps, "load_token", lambda: "tok")
    monkeypatch.setattr(ps, "stable_media_host", lambda: (lambda p: "https://cdn/v.mp4"))
    monkeypatch.setattr(
        ps, "publish",
        lambda *a, **k: {"twitter": {"id": "p1"}, "linkedin": {"error": "boom"}},
    )
    out = ps.publish_video(str(f), "cap")
    assert "twitter: published p1" in out
    assert "linkedin: FAILED — boom" in out


def test_publish_tool_missing_token(tmp_path, monkeypatch):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    monkeypatch.delenv("IRIS_PUBLISH_DIR", raising=False)
    monkeypatch.setattr(ps, "load_token", lambda: "")
    out = ps.publish_video(str(f), "cap")
    assert "IRIS_BUFFER_TOKEN" in out


def test_publish_tool_bad_when(tmp_path, monkeypatch):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    monkeypatch.delenv("IRIS_PUBLISH_DIR", raising=False)
    monkeypatch.setattr(ps, "load_token", lambda: "tok")
    out = ps.publish_video(str(f), "cap", when="not-a-date")
    assert "when" in out.lower() or "date" in out.lower()


def test_publish_tool_passes_schedule(tmp_path, monkeypatch):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    monkeypatch.delenv("IRIS_PUBLISH_DIR", raising=False)
    monkeypatch.setattr(ps, "load_token", lambda: "tok")
    monkeypatch.setattr(ps, "stable_media_host", lambda: (lambda p: "https://cdn/v.mp4"))
    seen = {}

    def fake_publish(mp4_path, caption, platforms, **k):
        seen["scheduled_at"] = k.get("scheduled_at")
        seen["platforms"] = platforms
        return {"twitter": {"id": "p1"}}

    monkeypatch.setattr(ps, "publish", fake_publish)
    ps.publish_video(str(f), "cap", platforms="twitter", when="2026-07-01T15:00:00")
    assert seen["scheduled_at"] == "2026-07-01T15:00:00"
    assert seen["platforms"] == ["twitter"]


def test_publish_dir_restriction(tmp_path, monkeypatch):
    inside = tmp_path / "out"
    inside.mkdir()
    good = inside / "v.mp4"
    good.write_bytes(b"x")
    outside = tmp_path / "other.mp4"
    outside.write_bytes(b"x")
    monkeypatch.setenv("IRIS_PUBLISH_DIR", str(inside))
    monkeypatch.setattr(ps, "load_token", lambda: "tok")
    monkeypatch.setattr(ps, "stable_media_host", lambda: (lambda p: "https://cdn/v.mp4"))
    monkeypatch.setattr(ps, "publish", lambda *a, **k: {"twitter": {"id": "p1"}})
    assert "Refused" in ps.publish_video(str(outside), "cap")
    assert "Refused" not in ps.publish_video(str(good), "cap")


def test_publish_tool_media_host_unconfigured(tmp_path, monkeypatch):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    monkeypatch.delenv("IRIS_PUBLISH_DIR", raising=False)
    monkeypatch.setattr(ps, "load_token", lambda: "tok")

    def boom():
        raise ps.BufferError("set IRIS_MEDIA_PUBLIC_BASE to a permanent public URL base")

    monkeypatch.setattr(ps, "stable_media_host", boom)
    out = ps.publish_video(str(f), "cap")
    assert "Media hosting is not configured" in out
    assert "IRIS_MEDIA_PUBLIC_BASE" in out
