"""Tests for the memory MCP server's tools against a temp store file.

These exercise the live tools (remember/recall/mark_useful/pin/set_importance/
forget) end to end on a real JSON file, since this server is relaunched fresh on
every turn and a broken tool breaks the next real message.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")  # the server needs the MCP SDK; skip if absent

from iris.mcp import memory_server as srv


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "MEMORY_FILE", tmp_path / "mem.json")
    return srv


def test_remember_then_recall(store):
    store.remember("Jason is a quant researcher", tags="bio", importance=5)
    out = store.recall("quant")
    assert "quant researcher" in out
    assert "imp5" in out


def test_recall_ranks_pinned_first(store):
    store.remember("trivia about nothing")
    store.remember("the home base fact", pinned=True)
    out = store.recall()  # browse, no query
    assert out.splitlines()[0].endswith("the home base fact")
    assert "PINNED" in out.splitlines()[0]


def test_mark_useful_increments(store):
    store.remember("useful note")
    assert "1x" in store.mark_useful(1)
    assert "2x" in store.mark_useful(1)
    assert store.mark_useful(999) == "No note #999."


def test_pin_and_unpin(store):
    store.remember("note")
    assert "Pinned" in store.pin(1)
    assert "PINNED" in store.recall()
    assert "Unpinned" in store.pin(1, False)
    assert "PINNED" not in store.recall()


def test_set_importance_clamps_and_persists(store):
    store.remember("note", importance=2)
    assert "to 5" in store.set_importance(1, 99)  # clamped to 5
    assert "imp5" in store.recall()
    assert store.set_importance(2, 3) == "No note #2."


def test_forget_removes(store):
    store.remember("disposable")
    assert "Deleted" in store.forget(1)
    assert store.recall() == "No notes saved yet."


def test_corrupt_memory_file_is_quarantined(store, tmp_path):
    (tmp_path / "mem.json").write_text("{not json", encoding="utf-8")
    out = store.recall("anything")  # must not crash on a corrupt store
    assert isinstance(out, str)
    assert (tmp_path / "mem.json.corrupt").exists()
