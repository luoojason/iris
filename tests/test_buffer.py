"""Tests for the Buffer publishing client. No network: HTTP is faked."""

from __future__ import annotations

import pytest

from iris.buffer import (
    BufferError,
    _graphql,
    create_post,
    list_channels,
    load_token,
    resolve_channels,
)


class FakeResp:
    def __init__(self, json_data=None, status_code=200, text=""):
        self._json = json_data or {}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._json


class FakeHttp:
    """Records POSTs and returns queued responses (Buffer is POST-only)."""

    def __init__(self, posts=()):
        self.post_q = list(posts)
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return self.post_q.pop(0)


def test_load_token_from_env(monkeypatch):
    monkeypatch.setenv("IRIS_BUFFER_TOKEN", "tok123")
    assert load_token() == "tok123"
    monkeypatch.delenv("IRIS_BUFFER_TOKEN", raising=False)
    assert load_token() == ""


def test_graphql_returns_data():
    http = FakeHttp(posts=[FakeResp({"data": {"ok": 1}})])
    out = _graphql("query { ok }", {}, token="t", http=http)
    assert out == {"ok": 1}
    # token goes on the Authorization header as a Bearer token
    _, kw = http.calls[0]
    assert "Bearer t" in kw["headers"]["Authorization"]


def test_graphql_raises_on_errors():
    http = FakeHttp(posts=[FakeResp({"errors": [{"message": "bad query"}]})])
    with pytest.raises(BufferError) as exc:
        _graphql("query { x }", {}, token="t", http=http)
    assert "bad query" in str(exc.value)


def test_graphql_raises_on_empty_data():
    http = FakeHttp(posts=[FakeResp({})])
    with pytest.raises(BufferError):
        _graphql("query { x }", {}, token="t", http=http)


def test_list_channels_parses():
    resp = FakeResp({"data": {"account": {"channels": [
        {"id": "c1", "service": "twitter", "handle": "@me"},
        {"id": "c2", "service": "linkedin", "handle": "me"},
    ]}}})
    chans = list_channels(token="t", http=FakeHttp(posts=[resp]))
    assert chans == [
        {"id": "c1", "service": "twitter", "handle": "@me"},
        {"id": "c2", "service": "linkedin", "handle": "me"},
    ]


CHANS = [
    {"id": "c1", "service": "twitter", "handle": "@me"},
    {"id": "c2", "service": "linkedin", "handle": "me"},
    {"id": "c3", "service": "youtube", "handle": "mychan"},
]


def test_resolve_channels_empty_means_all():
    ids, unknown = resolve_channels([], CHANS)
    assert ids == ["c1", "c2", "c3"]
    assert unknown == []


def test_resolve_channels_subset_by_service_case_insensitive():
    ids, unknown = resolve_channels(["Twitter", "LINKEDIN"], CHANS)
    assert ids == ["c1", "c2"]
    assert unknown == []


def test_resolve_channels_reports_unknown():
    ids, unknown = resolve_channels(["twitter", "tiktok"], CHANS)
    assert ids == ["c1"]
    assert unknown == ["tiktok"]


def test_create_post_now_with_video():
    http = FakeHttp(posts=[FakeResp({"data": {"createPost": {"id": "p1"}}})])
    out = create_post("hello", "c1", video_url="https://h/v.mp4", token="t", http=http)
    assert out == {"id": "p1"}
    _, kw = http.calls[0]
    variables = kw["json"]["variables"]
    assert variables["input"]["channelIds"] == ["c1"]
    assert variables["input"]["assets"][0]["video"]["url"] == "https://h/v.mp4"
    assert variables["input"].get("scheduledAt") is None


def test_create_post_scheduled():
    http = FakeHttp(posts=[FakeResp({"data": {"createPost": {"id": "p2"}}})])
    out = create_post(
        "hi", "c1", video_url="https://h/v.mp4",
        scheduled_at="2026-07-01T15:00:00", token="t", http=http,
    )
    assert out == {"id": "p2"}
    _, kw = http.calls[0]
    assert kw["json"]["variables"]["input"]["scheduledAt"] == "2026-07-01T15:00:00"


def test_create_post_error_is_failsoft():
    http = FakeHttp(posts=[FakeResp({"errors": [{"message": "channel down"}]})])
    out = create_post("hi", "c1", video_url="https://h/v.mp4", token="t", http=http)
    assert "error" in out and "channel down" in out["error"]


def test_create_post_missing_id_is_error():
    http = FakeHttp(posts=[FakeResp({"data": {"createPost": {}}})])
    out = create_post("hi", "c1", token="t", http=http)
    assert "error" in out
