"""Tests for the per-turn prompt-budget inspector (iris/prompt_size.py)."""

from __future__ import annotations

import json

from iris.config import Config
from iris.prompt_size import (
    JOBS,
    MCP_TOOLS,
    PERSONA,
    PINNED_MEMORY,
    STANDING_ORDERS,
    _probe_schema_bytes,
    measure,
    render,
)


def _config(tmp_path, **overrides) -> Config:
    """A Config whose state files all live under tmp_path (nothing real is touched)."""
    base = dict(
        persona_file=str(tmp_path / "persona.md"),
        standing_orders_file=str(tmp_path / "orders.md"),
        memory_file=str(tmp_path / "memory.json"),
        jobs_file=str(tmp_path / "jobs.json"),
        connections_file=str(tmp_path / "connections.json"),
    )
    base.update(overrides)
    return Config(**base)


def test_measure_reports_persona_and_standing_orders_byte_counts(tmp_path):
    persona = tmp_path / "persona.md"
    orders = tmp_path / "orders.md"
    persona.write_bytes(b"p" * 100)
    orders.write_bytes(b"o" * 250)
    config = _config(tmp_path)

    blocks = dict(measure(config))
    assert blocks[PERSONA] == 100
    assert blocks[STANDING_ORDERS] == 250


def test_measure_has_all_tier0_block_names(tmp_path):
    blocks = measure(_config(tmp_path))
    names = [name for name, _ in blocks]
    assert names == [PINNED_MEMORY, JOBS, PERSONA, STANDING_ORDERS, MCP_TOOLS]
    # Every block is an (str, int) pair.
    assert all(isinstance(n, str) and isinstance(v, int) for n, v in blocks)


def test_missing_files_contribute_zero_and_never_raise(tmp_path):
    # No persona, orders, memory, jobs, or connections files exist.
    blocks = dict(measure(_config(tmp_path)))
    assert blocks[PERSONA] == 0
    assert blocks[STANDING_ORDERS] == 0
    assert blocks[PINNED_MEMORY] == 0
    assert blocks[JOBS] == 0
    assert blocks[MCP_TOOLS] == 0


def test_pinned_memory_note_is_measured(tmp_path):
    # A pinned global note loads into the per-turn digest, so its block is > 0.
    note = {
        "id": 1,
        "text": "Jason prefers metric units",
        "tags": [],
        "pinned": True,
        "created_at": "2026-06-01T00:00:00Z",
    }
    (tmp_path / "memory.json").write_text(json.dumps([note]), encoding="utf-8")
    config = _config(tmp_path, memory_digest_bytes=2400)

    blocks = dict(measure(config))
    assert blocks[PINNED_MEMORY] > 0


def test_render_includes_blocks_and_total(tmp_path):
    (tmp_path / "persona.md").write_bytes(b"p" * 100)
    (tmp_path / "orders.md").write_bytes(b"o" * 250)
    out = render(_config(tmp_path))

    assert PERSONA in out
    assert STANDING_ORDERS in out
    assert "total" in out
    # The total line carries the sum of the measured blocks.
    total = sum(size for _, size in measure(_config(tmp_path)))
    assert str(total) in out
    assert total >= 350


class _FakeProc:
    """Replays canned JSON-RPC lines, mirroring tests/test_mcp_probe.py's fake."""

    def __init__(self, lines):
        self._lines = list(lines)

        class _In:
            def write(self, s):
                pass

            def flush(self):
                pass

            def close(self):
                pass

        class _Out:
            def __init__(self, lines):
                self._lines = lines

            def readline(self):
                return self._lines.pop(0) if self._lines else ""

            def close(self):
                pass

        self.stdin = _In()
        self.stdout = _Out(self._lines)
        self.stderr = _Out([])

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


def test_probe_schema_bytes_sizes_full_tool_list():
    # The MCP block sizes the FULL tool schema (name + description + inputSchema),
    # not just names, so a richer schema costs more than a bare one.
    init = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"capabilities": {}}}) + "\n"
    tool = {
        "name": "publish",
        "description": "Publish a post to a channel.",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    }
    tools = json.dumps({"jsonrpc": "2.0", "id": 2, "result": {"tools": [tool]}}) + "\n"

    class _Conn:
        command = "fake"
        args: list = []
        env: dict = {}

    got = _probe_schema_bytes(_Conn(), spawn=lambda cmd, env: _FakeProc([init, tools]))
    assert got == len(json.dumps(tool).encode("utf-8"))


def test_probe_schema_bytes_is_best_effort_on_dead_server():
    class _Conn:
        command = "fake"
        args: list = []
        env: dict = {}

    # A server that closes without responding contributes 0, never raises.
    got = _probe_schema_bytes(_Conn(), spawn=lambda cmd, env: _FakeProc([""]))
    assert got == 0
