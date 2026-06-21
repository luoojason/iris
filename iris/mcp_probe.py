"""Minimal MCP stdio probe: spawn a server and ask it for its tool list.

Used by ``iris mcp test`` so the owner can see which tools to allow. Speaks just
enough of MCP (JSON-RPC over stdio: initialize -> notifications/initialized ->
tools/list) to read tool names. Best-effort: any failure raises ProbeError.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Callable, Optional

PROTOCOL_VERSION = "2024-11-05"


class ProbeError(RuntimeError):
    pass


def _default_spawn(cmd, env):
    return subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
    )


def _send(proc, obj: dict) -> None:
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def _read_result(proc) -> dict:
    while True:
        line = proc.stdout.readline()
        if not line:
            raise ProbeError("server closed the connection before responding")
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "result" in msg or "error" in msg:
            if "error" in msg:
                raise ProbeError(str(msg["error"]))
            return msg["result"]


def probe_tools(command: str, args, env, *, timeout: float = 10.0,
                spawn: Optional[Callable] = None) -> list[str]:
    """Return the tool names an MCP server exposes, or raise ProbeError."""
    spawn = spawn or _default_spawn
    child_env = {**os.environ, **(env or {})}
    proc = spawn([command, *list(args)], child_env)
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {}, "clientInfo": {"name": "iris-probe", "version": "1"},
        }})
        _read_result(proc)
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        result = _read_result(proc)
        tools = result.get("tools", [])
        return [str(t.get("name", "")) for t in tools if t.get("name")]
    finally:
        try:
            proc.kill()
        except Exception:
            pass
