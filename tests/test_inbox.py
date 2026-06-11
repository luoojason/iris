"""Tests for the fold-back inbox (iris/inbox.py)."""

from __future__ import annotations

import json

from iris.inbox import INBOX_CAP, Inbox


def test_append_then_drain_returns_and_empties(tmp_path):
    box = Inbox(tmp_path / "inbox.json")
    box.append("job #1 finished: ok")
    box.append("wake build-errors: the build log hit an error")
    assert box.drain() == [
        "job #1 finished: ok",
        "wake build-errors: the build log hit an error",
    ]
    assert box.drain() == []  # drained means gone


def test_drain_on_missing_file_is_empty(tmp_path):
    assert Inbox(tmp_path / "never-written.json").drain() == []


def test_restore_puts_entries_back_at_the_front(tmp_path):
    box = Inbox(tmp_path / "inbox.json")
    box.append("first")
    drained = box.drain()
    box.append("second")  # arrived while the failed turn was in flight
    box.restore(drained)
    assert box.drain() == ["first", "second"]


def test_cap_drops_oldest(tmp_path):
    box = Inbox(tmp_path / "inbox.json")
    for n in range(INBOX_CAP + 5):
        box.append(f"entry {n}")
    entries = box.drain()
    assert len(entries) == INBOX_CAP
    assert entries[0] == "entry 5"  # the five oldest were dropped
    assert entries[-1] == f"entry {INBOX_CAP + 4}"


def test_corrupt_file_starts_fresh(tmp_path):
    path = tmp_path / "inbox.json"
    path.write_text("{not json", encoding="utf-8")
    box = Inbox(path)
    assert box.drain() == []
    box.append("after corruption")
    assert box.drain() == ["after corruption"]


def test_non_string_entries_are_ignored(tmp_path):
    path = tmp_path / "inbox.json"
    path.write_text(json.dumps(["good", 42, None, "also good"]), encoding="utf-8")
    assert Inbox(path).drain() == ["good", "also good"]
