"""Tests for the shared JSON state-store base (iris/statefile.py)."""

from __future__ import annotations

import json

from iris.statefile import JsonDictStore, JsonListStore


def test_list_store_roundtrip(tmp_path):
    s = JsonListStore(tmp_path / "x.json", "test list")
    assert s.load() == []  # missing file reads as the default
    with s.locked():
        s.save([{"a": 1}, {"b": 2}])
    assert s.load() == [{"a": 1}, {"b": 2}]


def test_list_store_corrupt_recovers_and_quarantines(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("{not json", "utf-8")
    s = JsonListStore(p, "test list")
    assert s.load() == []
    assert (tmp_path / "x.json.corrupt").exists()  # the bad file is preserved


def test_list_store_wrong_type_reads_as_default(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"not": "a list"}), "utf-8")
    assert JsonListStore(p, "t").load() == []


def test_dict_store_roundtrip(tmp_path):
    s = JsonDictStore(tmp_path / "d.json", "test dict")
    assert s.load() == {}
    with s.locked():
        s.save({"k": "v"})
    assert s.load() == {"k": "v"}


def test_dict_store_corrupt_recovers_and_quarantines(tmp_path):
    p = tmp_path / "d.json"
    p.write_text("nope", "utf-8")
    assert JsonDictStore(p, "t").load() == {}
    assert (tmp_path / "d.json.corrupt").exists()


def test_default_is_not_shared_between_calls(tmp_path):
    # A mutable default must be copied per load, never aliased.
    s = JsonListStore(tmp_path / "x.json", "t")
    a = s.load()
    a.append("mutated")
    assert s.load() == []  # the next load is unaffected


def test_locked_blocks_are_sequential(tmp_path):
    s = JsonListStore(tmp_path / "x.json", "t")
    with s.locked():
        s.save([1])
    with s.locked():
        s.save([1, 2])
    assert s.load() == [1, 2]
