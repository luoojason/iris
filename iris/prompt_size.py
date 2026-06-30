"""Inspect the fixed per-turn prompt budget: the bytes injected on every turn.

Before the model reads a single word of the owner's message, each turn already
pays for a set of always-on suppliers appended to the system prompt: the pinned
memory digest, the live jobs digest, the persona file, the standing-orders file,
and the schemas of any enabled MCP tools. Those bytes are re-billed on every
turn, so an owner who cannot see them cannot tell what is quietly eating the
context window.

This module measures each of those tier-0 suppliers and renders a per-block plus
total breakdown. It mirrors how ``iris/agent.py`` wires the same suppliers into
the driver (``pinned_digest``, ``jobs_digest``, ``persona_file``,
``standing_orders_file``), and reuses ``iris/mcp_probe.py`` to size live tool
schemas. Every supplier is best-effort: a missing file, an empty store, or an
unreachable MCP server contributes 0 and never raises, so the inspector is safe
to run in any environment.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Stable block labels, shared by ``measure`` and ``render`` so the breakdown
# table is a fixed shape the owner can scan turn over turn.
PINNED_MEMORY = "pinned memory digest"
JOBS = "jobs digest"
PERSONA = "persona file"
STANDING_ORDERS = "standing orders file"
MCP_TOOLS = "mcp tool schemas"

# Cap on how long the (optional) live MCP probe may spend per server. A probe
# that needs a server that is slow or absent fails best-effort to 0 well within
# this, so the inspector never hangs the owner's terminal.
_MCP_PROBE_TIMEOUT = 10.0


def _file_bytes(path: str | None) -> int:
    """Byte size of a prompt file, or 0 when it is unset or unreadable.

    The persona and standing-orders files are appended to the system prompt as
    their raw text, so the file's byte length is exactly what each turn re-bills.
    """
    if not path:
        return 0
    try:
        return len(Path(path).read_bytes())
    except OSError:
        return 0


def _memory_digest_bytes(config) -> int:
    """Bytes of the pinned-memory block injected each turn, scoped to none.

    Loads the memory store and renders it the way the agent does, at the
    configured budget. ``conversation_id=None`` measures the global floor that
    loads in every turn regardless of thread. Best-effort: any failure is 0.
    """
    try:
        budget = int(getattr(config, "memory_digest_bytes", 0) or 0)
        if budget <= 0:
            return 0
        from .memory import pinned_digest
        from .statefile import JsonListStore

        entries = JsonListStore(config.memory_file, "memory").load()
        text = pinned_digest(entries, time.time(), budget, conversation_id=None)
        return len(text.encode("utf-8"))
    except Exception:  # pragma: no cover - inspector must never raise
        return 0


def _jobs_digest_bytes(config) -> int:
    """Bytes of the live jobs digest injected each turn. Best-effort: 0 on failure.

    Read fresh from the job store, exactly like the agent's tier-0 supplier, so a
    deployment with several in-flight jobs sees the real per-turn cost of job
    awareness.
    """
    try:
        budget = int(getattr(config, "jobs_digest_bytes", 0) or 0)
        if budget <= 0:
            return 0
        from .jobs import jobs_digest
        from .statefile import JsonListStore

        jobs = JsonListStore(config.jobs_file, "job registry").load()
        recent = int(getattr(config, "jobs_digest_recent_secs", 3600) or 3600)
        text = jobs_digest(jobs, time.time(), budget, recent)
        return len(text.encode("utf-8"))
    except Exception:  # pragma: no cover - inspector must never raise
        return 0


def _probe_schema_bytes(conn, *, timeout: float = _MCP_PROBE_TIMEOUT, spawn=None) -> int:
    """Summed JSON byte size of one MCP server's tool schemas, or 0.

    Reuses ``iris.mcp_probe``'s stdio handshake primitives to read the full tool
    list (names plus descriptions plus input schemas) the server would inject,
    and sizes it as it would land in the prompt. Live and best-effort: an
    unreachable, slow, or misbehaving server contributes 0 and never raises.
    """
    from . import mcp_probe

    spawn = spawn or mcp_probe._default_spawn
    child_env = {**os.environ, **(getattr(conn, "env", None) or {})}
    try:
        proc = spawn([conn.command, *list(conn.args)], child_env)
    except Exception:
        return 0
    deadline = time.monotonic() + timeout
    try:
        mcp_probe._send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": mcp_probe.PROTOCOL_VERSION,
            "capabilities": {}, "clientInfo": {"name": "iris-prompt-size", "version": "1"},
        }})
        mcp_probe._read_result(proc, deadline)
        mcp_probe._send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        mcp_probe._send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        result = mcp_probe._read_result(proc, deadline)
        tools = (result or {}).get("tools", [])
        return sum(len(json.dumps(t).encode("utf-8")) for t in tools if isinstance(t, dict))
    except Exception:
        return 0
    finally:
        _close_proc(proc)


def _close_proc(proc) -> None:
    """Best-effort teardown of a probed server process, never raising."""
    try:
        proc.kill()
    except Exception:
        pass
    for name in ("stdin", "stdout", "stderr"):
        try:
            stream = getattr(proc, name, None)
            if stream is not None:
                stream.close()
        except Exception:
            pass
    try:
        proc.wait(timeout=2)
    except Exception:
        pass


def _mcp_schema_bytes(config, *, timeout: float = _MCP_PROBE_TIMEOUT, spawn=None) -> int:
    """Bytes of every enabled MCP server's tool schemas. Best-effort: 0 on failure.

    Optional by design: sizing real schemas needs a live server, so when no
    connections file exists, no connection is enabled, or probing fails, this
    contributes 0 and the inspector still reports the rest of the budget.
    """
    try:
        cfile = getattr(config, "connections_file", "") or ""
        if not cfile or not Path(cfile).exists():
            return 0
        from .connections import ConnectionStore

        conns = [c for c in ConnectionStore(cfile).list() if c.enabled]
        return sum(_probe_schema_bytes(c, timeout=timeout, spawn=spawn) for c in conns)
    except Exception:  # pragma: no cover - inspector must never raise
        return 0


def measure(config) -> list[tuple[str, int]]:
    """Byte size of each fixed per-turn supplier, as ``[(block_name, bytes), ...]``.

    Covers the tier-0 suppliers the driver appends on every turn: the pinned
    memory digest, the live jobs digest, the persona file, the standing-orders
    file, and (best-effort, live) the enabled MCP tool schemas. The block set is
    fixed so the breakdown is stable; a missing file or unavailable supplier
    reports 0 rather than dropping out or raising.
    """
    return [
        (PINNED_MEMORY, _memory_digest_bytes(config)),
        (JOBS, _jobs_digest_bytes(config)),
        (PERSONA, _file_bytes(getattr(config, "persona_file", None))),
        (STANDING_ORDERS, _file_bytes(getattr(config, "standing_orders_file", None))),
        (MCP_TOOLS, _mcp_schema_bytes(config)),
    ]


def render(config) -> str:
    """A per-block plus total breakdown table of the fixed per-turn prompt budget."""
    rows = measure(config)
    label_width = max([len(name) for name, _ in rows] + [len("total")])
    byte_width = max([len(str(size)) for _, size in rows] + [len(str(sum(s for _, s in rows))), len("bytes")])
    header = f"{'block'.ljust(label_width)}  {'bytes'.rjust(byte_width)}"
    rule = "-" * len(header)
    lines = [header, rule]
    total = 0
    for name, size in rows:
        total += size
        lines.append(f"{name.ljust(label_width)}  {str(size).rjust(byte_width)}")
    lines.append(rule)
    lines.append(f"{'total'.ljust(label_width)}  {str(total).rjust(byte_width)}")
    return "\n".join(lines)
