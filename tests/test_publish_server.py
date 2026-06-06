"""Tests for the publish MCP tool (the underlying publishing is faked)."""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from iris.mcp import publish_server as ps


def test_publish_tool_missing_file(monkeypatch):
    assert "No such file" in ps.publish_video("/nope/x.mp4", "cap")


def test_publish_tool_formats_results(tmp_path, monkeypatch):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    monkeypatch.setattr(ps, "SocialTokens", type("T", (), {"load": staticmethod(lambda *a, **k: object())}))
    monkeypatch.setattr(
        ps, "_publish_video",
        lambda *a, **k: {"youtube": {"id": "V", "url": "https://youtu.be/V"}, "instagram": {"error": "boom"}},
    )
    out = ps.publish_video(str(f), "cap", platforms="youtube,instagram")
    assert "youtube: published https://youtu.be/V" in out
    assert "instagram: FAILED — boom" in out
