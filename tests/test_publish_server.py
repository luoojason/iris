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
    monkeypatch.setenv("IRIS_PUBLISH_DIR", str(tmp_path))  # publishing requires an opt-in dir
    monkeypatch.setattr(ps, "SocialTokens", type("T", (), {"load": staticmethod(lambda *a, **k: object())}))
    monkeypatch.setattr(
        ps, "_publish_video",
        lambda *a, **k: {"youtube": {"id": "V", "url": "https://youtu.be/V"}, "instagram": {"error": "boom"}},
    )
    out = ps.publish_video(str(f), "cap", platforms="youtube,instagram")
    assert "youtube: published https://youtu.be/V" in out
    assert "instagram: FAILED — boom" in out


def test_publish_defaults_to_unlisted(tmp_path, monkeypatch):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    monkeypatch.setenv("IRIS_PUBLISH_DIR", str(tmp_path))  # publishing requires an opt-in dir
    monkeypatch.setattr(ps, "SocialTokens", type("T", (), {"load": staticmethod(lambda *a, **k: object())}))
    seen = {}

    def fake(*a, **k):
        seen["privacy"] = k.get("privacy")
        return {"youtube": {"id": "V", "url": "u"}}

    monkeypatch.setattr(ps, "_publish_video", fake)
    ps.publish_video(str(f), "cap", platforms="youtube")
    assert seen["privacy"] == "unlisted"  # not public unless asked


def test_publish_refuses_when_publish_dir_unset(tmp_path, monkeypatch):
    # Publishing is irreversible and public; with no IRIS_PUBLISH_DIR set it must
    # fail closed rather than letting a confused/injected turn post any mp4 on the box.
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    monkeypatch.delenv("IRIS_PUBLISH_DIR", raising=False)

    def must_not_publish(*a, **k):
        raise AssertionError("publish must not run with IRIS_PUBLISH_DIR unset")

    monkeypatch.setattr(ps, "_publish_video", must_not_publish)
    out = ps.publish_video(str(f), "cap", platforms="youtube")
    assert "Refused" in out and "IRIS_PUBLISH_DIR" in out


def test_publish_dir_restriction(tmp_path, monkeypatch):
    inside = tmp_path / "out"
    inside.mkdir()
    good = inside / "v.mp4"
    good.write_bytes(b"x")
    outside = tmp_path / "other.mp4"
    outside.write_bytes(b"x")
    monkeypatch.setenv("IRIS_PUBLISH_DIR", str(inside))
    monkeypatch.setattr(ps, "SocialTokens", type("T", (), {"load": staticmethod(lambda *a, **k: object())}))
    monkeypatch.setattr(ps, "_publish_video", lambda *a, **k: {"youtube": {"id": "V", "url": "u"}})
    assert "Refused" in ps.publish_video(str(outside), "cap", platforms="youtube")
    assert "Refused" not in ps.publish_video(str(good), "cap", platforms="youtube")
