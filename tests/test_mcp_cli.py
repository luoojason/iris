"""Tests for the `iris mcp` CLI. Operates on a temp connections file."""

from __future__ import annotations

import argparse
import json

from iris.cli import build_parser, mcp_command
from iris.config import Config
from iris.connections import ConnectionStore


def cfg(tmp_path):
    return Config(connections_file=str(tmp_path / "conns.json"))


def run(tmp_path, *argv):
    args = build_parser().parse_args(["mcp", *argv])
    return mcp_command(args, cfg(tmp_path))


def test_add_then_list(tmp_path, capsys):
    assert run(tmp_path, "add", "buffer", "--command", "npx", "--arg", "buffer-mcp",
               "--env", "TOKEN=abc", "--allow", "mcp__buffer__publish") == 0
    s = ConnectionStore(str(tmp_path / "conns.json"))
    c = s.get("buffer")
    assert c.command == "npx" and c.args == ["buffer-mcp"]
    assert c.env == {"TOKEN": "abc"} and c.allowed_tools == ["mcp__buffer__publish"]
    assert run(tmp_path, "list") == 0
    out = capsys.readouterr().out
    assert "buffer" in out and "abc" not in out  # secret never printed


def test_allow_all_uses_server_prefix(tmp_path):
    run(tmp_path, "add", "fs", "--command", "fs-mcp", "--allow-all")
    c = ConnectionStore(str(tmp_path / "conns.json")).get("fs")
    assert c.allowed_tools == ["mcp__fs"]


def test_enable_disable_remove(tmp_path):
    run(tmp_path, "add", "x", "--command", "c")
    assert ConnectionStore(str(tmp_path / "conns.json")).get("x").enabled is True
    assert run(tmp_path, "disable", "x") == 0
    assert ConnectionStore(str(tmp_path / "conns.json")).get("x").enabled is False
    assert run(tmp_path, "enable", "x") == 0
    assert ConnectionStore(str(tmp_path / "conns.json")).get("x").enabled is True
    assert run(tmp_path, "remove", "x") == 0
    assert ConnectionStore(str(tmp_path / "conns.json")).get("x") is None


def test_remove_missing_returns_nonzero(tmp_path):
    assert run(tmp_path, "remove", "nope") == 1


def test_import_registers_disabled(tmp_path):
    src = tmp_path / "mcp.json"
    src.write_text(json.dumps({"mcpServers": {
        "memory": {"command": "python", "args": ["-m", "iris.mcp.memory_server"], "env": {"X": "1"}}
    }}))
    assert run(tmp_path, "import", str(src)) == 0
    c = ConnectionStore(str(tmp_path / "conns.json")).get("memory")
    assert c is not None and c.enabled is False  # imported disabled until owner enables + allows
    assert c.command == "python" and c.args == ["-m", "iris.mcp.memory_server"]
