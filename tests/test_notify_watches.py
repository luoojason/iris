"""Tests for the change-watch store."""

from __future__ import annotations

from iris.notify.watches import WatchStore, new_watch


def test_new_watch_defaults():
    w = new_watch("n", cmd="echo hi")
    assert w["cmd"] == "echo hi" and w["url"] is None
    assert w["extract"] == {"kind": "text", "arg": ""}
    assert w["last_value"] is None
    assert w["every_seconds"] == 0.0


def test_add_get_list_remove(tmp_path):
    s = WatchStore(tmp_path / "w.json")
    s.add(new_watch("blog", url="http://x"))
    assert s.get("blog")["url"] == "http://x"
    assert [w["name"] for w in s.list()] == ["blog"]
    assert s.remove("blog") is True
    assert s.get("blog") is None
    assert s.remove("blog") is False


def test_persists_and_records(tmp_path):
    p = tmp_path / "w.json"
    s = WatchStore(p)
    s.add(new_watch("v", url="http://x"))
    s.record("v", "1.2.3", 100.0, changed=True)
    reloaded = WatchStore(p)
    w = reloaded.get("v")
    assert w["last_value"] == "1.2.3"
    assert w["last_checked"] == 100.0
    assert w["last_changed"] == 100.0


def test_due_respects_every_seconds(tmp_path):
    s = WatchStore(tmp_path / "w.json")
    s.add(new_watch("fast", url="http://x", every_seconds=0))
    s.add(new_watch("hourly", url="http://y", every_seconds=3600))
    s.record("hourly", "v", 1000.0, changed=False)
    due = [w["name"] for w in s.due(now=1500.0)]
    assert "fast" in due
    assert "hourly" not in due
    assert "hourly" in [w["name"] for w in s.due(now=5000.0)]
