"""Session store tests."""

from __future__ import annotations

from iris.sessions import SessionStore


def test_set_get_roundtrip(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    assert store.get("chan-1") is None
    store.set("chan-1", "session-abc")
    assert store.get("chan-1") == "session-abc"


def test_persists_across_instances(tmp_path):
    path = tmp_path / "s.json"
    SessionStore(path).set("chan-1", "sess-1")
    assert SessionStore(path).get("chan-1") == "sess-1"


def test_clear_forgets_conversation(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    store.set("chan-1", "sess-1")
    assert store.clear("chan-1") is True
    assert store.get("chan-1") is None
    assert store.clear("chan-1") is False


def test_corrupt_file_recovers(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = SessionStore(path)  # must not raise
    assert store.get("anything") is None
    assert path.with_suffix(".json.corrupt").exists()
    # still usable afterwards
    store.set("chan", "sess")
    assert SessionStore(path).get("chan") == "sess"


def test_set_updates_existing(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    store.set("chan", "old")
    store.set("chan", "new")
    assert store.get("chan") == "new"
    assert len(store.all()) == 1


def test_turns_counts_same_session_and_resets_on_change(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    assert store.turns("chan") == 0
    store.set("chan", "sess-1")
    assert store.turns("chan") == 1
    store.set("chan", "sess-1")  # same session, another turn
    store.set("chan", "sess-1")
    assert store.turns("chan") == 3
    store.set("chan", "sess-2")  # a new session resets the count
    assert store.turns("chan") == 1
