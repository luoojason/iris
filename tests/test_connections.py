"""Tests for the MCP connection registry. No real servers; file-backed only."""

from __future__ import annotations

import pytest

from iris.connections import Connection, ConnectionStore, valid_name


def test_valid_name():
    assert valid_name("buffer")
    assert valid_name("my-pub_1")
    assert not valid_name("Bad Name")
    assert not valid_name("")
    assert not valid_name("-leading")


def store(tmp_path):
    return ConnectionStore(str(tmp_path / "conns.json"))


def test_add_and_get(tmp_path):
    s = store(tmp_path)
    c = s.add("buffer", "npx", args=["buffer-mcp"], env={"TOKEN": "x"}, allowed_tools=["mcp__buffer__publish"])
    assert isinstance(c, Connection)
    assert c.name == "buffer" and c.command == "npx" and c.enabled is True
    got = s.get("buffer")
    assert got.args == ["buffer-mcp"] and got.env == {"TOKEN": "x"}


def test_add_rejects_bad_name_dup_and_empty_command(tmp_path):
    s = store(tmp_path)
    s.add("buffer", "npx")
    with pytest.raises(ValueError):
        s.add("buffer", "npx")  # duplicate
    with pytest.raises(ValueError):
        s.add("Bad Name", "npx")  # bad name
    with pytest.raises(ValueError):
        s.add("ok", "")  # empty command


def test_list_sorted_and_remove(tmp_path):
    s = store(tmp_path)
    s.add("zeta", "a")
    s.add("alpha", "b")
    assert [c.name for c in s.list()] == ["alpha", "zeta"]
    assert s.remove("alpha") is True
    assert s.remove("nope") is False
    assert [c.name for c in s.list()] == ["zeta"]


def test_set_enabled(tmp_path):
    s = store(tmp_path)
    s.add("buffer", "npx")
    s.set_enabled("buffer", False)
    assert s.get("buffer").enabled is False
    with pytest.raises(ValueError):
        s.set_enabled("missing", True)


def test_to_mcp_config_and_allowed_tools_enabled_only(tmp_path):
    s = store(tmp_path)
    s.add("a", "cmda", args=["x"], env={"K": "v"}, allowed_tools=["mcp__a__one", "mcp__a__two"])
    s.add("b", "cmdb", allowed_tools=["mcp__b__go"], enabled=False)
    cfg = s.to_mcp_config()
    assert cfg == {"mcpServers": {"a": {"command": "cmda", "args": ["x"], "env": {"K": "v"}}}}
    assert s.allowed_tools_for_enabled() == ["mcp__a__one", "mcp__a__two"]
