"""Tests for the memory MCP tool (storage logic)."""

from __future__ import annotations

import importlib

import pytest

pytest.importorskip("mcp")


def _fresh(tmp_path, monkeypatch, seed=None):
    path = tmp_path / "mem.json"
    if seed is not None:
        path.write_text(seed, encoding="utf-8")
    monkeypatch.setenv("IRIS_MEMORY_FILE", str(path))
    from iris.mcp import memory_server as m
    importlib.reload(m)  # MEMORY_FILE is read at import
    return m


def test_memory_roundtrip(tmp_path, monkeypatch):
    m = _fresh(tmp_path, monkeypatch)
    assert "Saved note #1" in m.remember("likes green", tags="prefs,color")
    assert "Saved note #2" in m.remember("works at night")
    assert "green" in m.recall("green")
    assert "No matching" in m.recall("zzz")
    assert "Deleted note #1" in m.forget(1)
    assert "No note #1" in m.forget(1)


def test_memory_tolerates_legacy_notes(tmp_path, monkeypatch):
    m = _fresh(tmp_path, monkeypatch, seed='[{"id": 1, "text": "old note"}]')
    out = m.recall()  # must not KeyError on missing tags/created_at
    assert "old note" in out
    # next id is max(existing)+1, not len-based
    assert "Saved note #2" in m.remember("new")
