"""Tests for webhook wakes (iris/webhooks.py): the testable core of the inbound
HTTP listener. The socket layer is a thin wrapper, not unit-tested here."""

from __future__ import annotations

from iris.config import Config
from iris.inbox import Inbox
from iris.webhooks import _authorized, build_message, handle_hook


def test_authorized_is_constant_time_and_rejects_empties():
    assert _authorized("secret", "secret") is True
    assert _authorized("wrong", "secret") is False
    assert _authorized("secret", "") is False   # no token configured -> never authorized
    assert _authorized("", "secret") is False
    assert _authorized(None, "secret") is False


def test_build_message_from_plain_body_and_json():
    assert build_message("deploy", "build finished") == "webhook deploy: build finished"
    assert build_message("ci", '{"message": "tests passed"}') == "webhook ci: tests passed"
    assert build_message("", "") == "webhook hook"  # name defaults, empty body ok


def test_build_message_truncates_a_huge_body():
    out = build_message("x", "y" * 5000)
    assert len(out) < 1100


def test_build_message_caps_an_oversized_name():
    # the URL path segment becomes the name; it must be bounded like the body so a
    # huge path can't inflate the inbox note.
    out = build_message("z" * 5000, "ok")
    assert len(out) < 1100


def _cfg(tmp_path, **kw):
    base = dict(webhook_enabled=True, webhook_token="s3cret", home_channel="home-1",
                discord_token="tok", inbox_file=str(tmp_path / "inbox.json"))
    base.update(kw)
    return Config(**base)


def test_handle_hook_rejected_when_disabled(tmp_path):
    sent = []
    status, _ = handle_hook(_cfg(tmp_path, webhook_enabled=False), name="x", body="b",
                            token="s3cret", sender=lambda c, t, k: sent.append(t))
    assert status == 404 and sent == []


def test_handle_hook_refuses_without_a_configured_token(tmp_path):
    status, text = handle_hook(_cfg(tmp_path, webhook_token=""), name="x", body="b",
                               token="anything", sender=lambda c, t, k: None)
    assert status == 503 and "token" in text.lower()


def test_handle_hook_rejects_a_bad_token(tmp_path):
    sent = []
    status, _ = handle_hook(_cfg(tmp_path), name="x", body="b", token="wrong",
                            sender=lambda c, t, k: sent.append(t))
    assert status == 401 and sent == []


def test_handle_hook_delivers_an_authorized_post(tmp_path):
    cfg = _cfg(tmp_path)
    sent = []
    status, _ = handle_hook(cfg, name="deploy", body="shipped v2", token="s3cret",
                            sender=lambda c, t, k: sent.append((c, t)))
    assert status == 200
    assert sent and sent[0][0] == "home-1" and "shipped v2" in sent[0][1]
    # folded into the inbox for the next turn too
    assert any("shipped v2" in n for n in Inbox(cfg.inbox_file).drain("discord:home-1"))


def test_handle_hook_routes_to_the_webhook_channel_when_set(tmp_path):
    cfg = _cfg(tmp_path, webhook_channel="ops-7")
    sent = []
    handle_hook(cfg, name="x", body="b", token="s3cret",
                sender=lambda c, t, k: sent.append((c, t)))
    assert sent[0][0] == "ops-7"
