"""Tests for social publishing. No network, no real accounts: HTTP is faked."""

from __future__ import annotations

import json

from iris.social import (
    SocialTokens,
    publish_instagram,
    publish_video,
    publish_youtube,
    youtube_access_token,
)


class FakeResp:
    def __init__(self, json_data=None, headers=None, status_code=200, text=""):
        self._json = json_data or {}
        self.headers = headers or {}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._json


class FakeHttp:
    def __init__(self, posts=(), gets=(), puts=()):
        self.post_q = list(posts)
        self.get_q = list(gets)
        self.put_q = list(puts)
        self.calls = []

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.post_q.pop(0)

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self.get_q.pop(0)

    def put(self, url, **kw):
        self.calls.append(("PUT", url, kw))
        return self.put_q.pop(0)


YT = SocialTokens(yt_client_id="c", yt_client_secret="s", yt_refresh_token="r")
IG = SocialTokens(ig_user_id="123", ig_access_token="tok")


def test_tokens_load_and_predicates(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"yt_client_id": "c", "yt_client_secret": "s", "yt_refresh_token": "r"}))
    t = SocialTokens.load(str(p))
    assert t.has_youtube() and not t.has_instagram()
    assert SocialTokens.load(str(tmp_path / "missing.json")).has_youtube() is False


def test_youtube_access_token_refresh():
    http = FakeHttp(posts=[FakeResp({"access_token": "AT"})])
    assert youtube_access_token(YT, http=http) == "AT"


def test_youtube_access_token_failure_raises():
    from iris.social import PublishError
    http = FakeHttp(posts=[FakeResp({"error": "invalid_grant"})])
    try:
        youtube_access_token(YT, http=http)
        assert False, "should have raised"
    except PublishError as exc:
        assert "invalid_grant" in str(exc)


def test_publish_youtube_resumable_upload():
    http = FakeHttp(
        posts=[
            FakeResp({"access_token": "AT"}),                                  # token refresh
            FakeResp({}, headers={"Location": "https://upload.example/sess"}),  # init -> Location
        ],
        puts=[FakeResp({"id": "VID123"})],                                     # the upload itself
    )
    out = publish_youtube("/x.mp4", "My Short #Shorts", tokens=YT, http=http, read_file=lambda p: b"data")
    assert out == {"id": "VID123", "url": "https://youtu.be/VID123"}
    # the bytes were PUT to the session URL the init returned
    assert http.calls[-1][0] == "PUT" and http.calls[-1][1] == "https://upload.example/sess"


def test_publish_youtube_no_upload_url_is_error():
    http = FakeHttp(posts=[FakeResp({"access_token": "AT"}), FakeResp({}, headers={}, status_code=403, text="forbidden")])
    out = publish_youtube("/x.mp4", "t", tokens=YT, http=http, read_file=lambda p: b"d")
    assert "error" in out and "upload URL" in out["error"]


def test_publish_instagram_container_poll_publish():
    http = FakeHttp(
        posts=[FakeResp({"id": "CONTArn"}), FakeResp({"id": "MEDIA1"})],  # create container, then publish
        gets=[FakeResp({"status_code": "FINISHED"})],                     # poll -> ready
    )
    out = publish_instagram("https://pub/v.mp4", "cap", tokens=IG, http=http, sleep=lambda s: None)
    assert out == {"id": "MEDIA1"}


def test_publish_instagram_transcode_error():
    http = FakeHttp(posts=[FakeResp({"id": "C1"})], gets=[FakeResp({"status_code": "ERROR"})])
    out = publish_instagram("https://pub/v.mp4", "cap", tokens=IG, http=http, sleep=lambda s: None)
    assert "error" in out


def test_publish_video_reports_missing_config():
    out = publish_video("/x.mp4", "cap", ["youtube", "instagram", "tiktok"], tokens=SocialTokens())
    assert "not configured" in out["youtube"]["error"]
    assert "not configured" in out["instagram"]["error"]
    assert "not implemented" in out["tiktok"]["error"]


def test_publish_video_instagram_without_host_errors():
    out = publish_video("/x.mp4", "cap", ["instagram"], tokens=IG, media_host=None)
    assert "no media_host" in out["instagram"]["error"]


def test_publish_video_dispatches_instagram_with_host():
    http = FakeHttp(
        posts=[FakeResp({"id": "C1"}), FakeResp({"id": "M1"})],
        gets=[FakeResp({"status_code": "FINISHED"})],
    )
    out = publish_video(
        "/x.mp4", "cap", ["instagram"], tokens=IG,
        media_host=lambda path: "https://pub/v.mp4", http=http, sleep=lambda s: None,
    )
    assert out["instagram"] == {"id": "M1"}
