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


def test_notes_are_scoped_to_their_conversation(tmp_path):
    # The bug fix: a note tagged for one conversation must not surface in another.
    box = Inbox(tmp_path / "inbox.json")
    box.append("knicks job finished", conversation_id="discord:AAA")
    box.append("trae young job finished", conversation_id="discord:BBB")
    assert box.drain("discord:BBB") == ["trae young job finished"]   # only B's note
    assert box.drain("discord:AAA") == ["knicks job finished"]        # A's still there
    assert box.drain("discord:AAA") == []                             # now drained


def test_drain_of_one_conversation_leaves_others_intact(tmp_path):
    box = Inbox(tmp_path / "inbox.json")
    box.append("for home", conversation_id="discord:HOME")
    box.append("untagged legacy")  # conversation_id None
    assert box.drain("discord:OTHER") == []          # nothing for an unrelated thread
    assert box.drain() == ["untagged legacy"]         # drain(None) takes the legacy one
    assert box.drain("discord:HOME") == ["for home"]  # home note untouched by the above


def test_restore_keeps_restored_entries_when_at_cap(tmp_path):
    # A failed turn must not lose its fold-back notes even when the inbox has
    # since filled to the cap: the restored entries belong at the front and the
    # oldest tail entries are the ones that should be dropped, not the restore.
    box = Inbox(tmp_path / "inbox.json")
    drained = ["restore me 1", "restore me 2"]
    for n in range(INBOX_CAP):
        box.append(f"other {n}")
    box.restore(drained)
    survivors = box.drain()
    assert len(survivors) == INBOX_CAP
    assert survivors[0] == "restore me 1"
    assert survivors[1] == "restore me 2"
    assert "restore me 1" in survivors and "restore me 2" in survivors


def test_restore_keeps_the_conversation_tag(tmp_path):
    box = Inbox(tmp_path / "inbox.json")
    box.append("a", conversation_id="discord:X")
    drained = box.drain("discord:X")
    box.restore(drained, "discord:X")
    assert box.drain("discord:Y") == []               # didn't leak to Y
    assert box.drain("discord:X") == ["a"]
