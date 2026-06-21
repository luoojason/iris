"""Tests for the MCP stdio probe, using a fake server process."""

from __future__ import annotations

import json

import pytest

from iris.mcp_probe import ProbeError, probe_tools


class FakeProc:
    """Replays canned JSON-RPC responses; records what was written to stdin."""

    def __init__(self, responses):
        self._lines = list(responses)
        self.written = []

        class _In:
            def __init__(self, outer): self._o = outer
            def write(self, s): self._o.written.append(s)
            def flush(self): pass
            def close(self): pass

        class _Out:
            def __init__(self, lines): self._lines = lines
            def readline(self):
                return self._lines.pop(0) if self._lines else ""

        self.stdin = _In(self)
        self.stdout = _Out(self._lines)
        self.stderr = _Out([])

    def poll(self): return None
    def wait(self, timeout=None): return 0
    def kill(self): pass


def fake_spawn(responses):
    def _spawn(cmd, env):
        return FakeProc(responses)
    return _spawn


def test_probe_returns_tool_names():
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}) + "\n"
    tools = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": [
        {"name": "publish"}, {"name": "list_channels"}]}}) + "\n"
    got = probe_tools("npx", ["buffer-mcp"], {}, spawn=fake_spawn([init, tools]))
    assert got == ["publish", "list_channels"]


def test_probe_raises_on_empty():
    with pytest.raises(ProbeError):
        probe_tools("bad", [], {}, spawn=fake_spawn([""]))
