"""Tests for the change-watch tick engine (fetch and delivery faked)."""

from __future__ import annotations

from iris.config import Config
from iris.notify import watch_tick
from iris.notify.watches import WatchStore, new_watch


def cfg():
    return Config(discord_token="t", notify_channel="123", watch_min_seconds=30)


def collect(sent):
    def sender(channel, text, token):
        sent.append((channel, text, token))
        return True
    return sender


def test_first_sighting_is_silent_baseline(tmp_path):
    s = WatchStore(tmp_path / "w.json")
    s.add(new_watch("v", url="http://x"))
    sent = []
    checked, changed = watch_tick.tick(s, cfg(), now=10.0,
                                       http_get=lambda url: (200, "1.0"), sender=collect(sent))
    assert (checked, changed) == (1, 0)
    assert sent == []
    assert s.get("v")["last_value"] == "1.0"


def test_unchanged_is_silent(tmp_path):
    s = WatchStore(tmp_path / "w.json")
    s.add(new_watch("v", url="http://x"))
    s.record("v", "1.0", 0.0, changed=False)
    sent = []
    checked, changed = watch_tick.tick(s, cfg(), now=10.0,
                                       http_get=lambda url: (200, "1.0"), sender=collect(sent))
    assert (checked, changed) == (1, 0)
    assert sent == []


def test_change_notifies(tmp_path):
    s = WatchStore(tmp_path / "w.json")
    s.add(new_watch("v", url="http://x"))
    s.record("v", "1.0", 0.0, changed=False)
    sent = []
    checked, changed = watch_tick.tick(s, cfg(), now=10.0,
                                       http_get=lambda url: (200, "2.0"), sender=collect(sent))
    assert (checked, changed) == (1, 1)
    assert sent == [("123", "changed: v is now 2.0 (was 1.0)", "t")]
    assert s.get("v")["last_value"] == "2.0"


def test_every_seconds_throttles(tmp_path):
    s = WatchStore(tmp_path / "w.json")
    s.add(new_watch("v", url="http://x", every_seconds=3600))
    s.record("v", "1.0", 1000.0, changed=False)
    sent = []
    checked, changed = watch_tick.tick(s, cfg(), now=1500.0,
                                       http_get=lambda url: (200, "2.0"), sender=collect(sent))
    assert (checked, changed) == (0, 0)
    assert sent == []


def test_change_falls_back_to_print_when_no_delivery(tmp_path, capsys):
    s = WatchStore(tmp_path / "w.json")
    s.add(new_watch("v", url="http://x"))
    s.record("v", "1.0", 0.0, changed=False)
    checked, changed = watch_tick.tick(s, Config(), now=10.0,
                                       http_get=lambda url: (200, "2.0"))
    assert (checked, changed) == (1, 1)
    assert "changed: v is now 2.0 (was 1.0)" in capsys.readouterr().out


def test_make_watch_from_flags_status():
    w = watch_tick.make_watch_from_flags("s", url="http://x", status=True)
    assert w["extract"] == {"kind": "status", "arg": ""}


def test_make_watch_from_flags_json():
    w = watch_tick.make_watch_from_flags("j", url="http://x", json_key="a.b")
    assert w["extract"] == {"kind": "json", "arg": "a.b"}
