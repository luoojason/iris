"""Tests for the session/history search MCP tool."""

from __future__ import annotations

import importlib
import json

import pytest

pytest.importorskip("mcp")


def _load(tmp_path, monkeypatch, raw_text):
    proj = tmp_path / "projects" / "proj"
    proj.mkdir(parents=True)
    (proj / "session.jsonl").write_text(raw_text, encoding="utf-8")
    monkeypatch.setenv("IRIS_TRANSCRIPTS_DIR", str(tmp_path / "projects"))
    from iris.mcp import session_search as s
    importlib.reload(s)
    return s


def _jsonl(*objs):
    return "\n".join(json.dumps(o) for o in objs)


def test_search_finds_text_in_str_and_block_content(tmp_path, monkeypatch):
    s = _load(tmp_path, monkeypatch, _jsonl(
        {"message": {"role": "user", "content": "I love the color teal"}, "timestamp": "t1"},
        {"message": {"role": "assistant", "content": [{"type": "text", "text": "Noted, teal it is"}]}, "timestamp": "t2"},
        {"message": {"role": "user", "content": "something unrelated"}, "timestamp": "t3"},
    ))
    out = s.search_history("teal")
    assert "I love the color teal" in out
    assert "Noted, teal" in out
    assert "unrelated" not in out
    assert "No past messages" in s.search_history("zzzz")


def test_skips_malformed_lines(tmp_path, monkeypatch):
    s = _load(tmp_path, monkeypatch,
              'not json at all\n' + json.dumps({"message": {"role": "user", "content": "hello world"}, "timestamp": "t"}) + "\n")
    assert "hello world" in s.search_history("hello")


def test_recent_history(tmp_path, monkeypatch):
    s = _load(tmp_path, monkeypatch, _jsonl(
        {"message": {"role": "user", "content": "first thing"}, "timestamp": "t1"},
        {"message": {"role": "assistant", "content": "second thing"}, "timestamp": "t2"},
    ))
    out = s.recent_history(limit=5)
    assert "first thing" in out and "second thing" in out


def test_rank_history_orders_by_relevance_not_recency(tmp_path, monkeypatch):
    s = _load(tmp_path, monkeypatch, "")
    messages = [
        ("user", "newest message about cats", "t3"),         # newest, but 0 query terms
        ("user", "the weekly usage cap on the plan", "t2"),  # all 3 query terms
        ("user", "cap cap cap cap", "t1"),                   # 1 term, repeated
    ]
    ranked = s.rank_history(messages, "weekly usage cap", limit=5)
    assert ranked[0][1] == "the weekly usage cap on the plan"  # most distinct terms wins
    assert all("cats" not in text for _, text, _ in ranked)    # zero-match dropped


def test_rank_history_empty_query_returns_nothing(tmp_path, monkeypatch):
    s = _load(tmp_path, monkeypatch, "")
    assert s.rank_history([("user", "anything", "t")], "", limit=5) == []
