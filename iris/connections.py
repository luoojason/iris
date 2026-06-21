"""Owner-registered MCP connections — the "bring your own MCP server" registry.

The model never writes this file. The owner registers MCP servers under short
names with ``iris mcp add``; at runtime the driver's mcp-config and allowlist
are derived from the enabled connections (see ``resolve_connections``). Built-in
Iris servers are just connections the owner may choose to enable; nothing is
pre-set. Mirrors the owner-CLI-only writer model of ``iris/workspaces.py``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from .statefile import JsonDictStore

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def valid_name(name: str) -> bool:
    """Whether a connection name is well-formed (short, lowercase, no paths)."""
    return bool(_NAME.match(name or ""))


@dataclass
class Connection:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    enabled: bool = True


class ConnectionStore:
    """Registry of name -> MCP server spec, owner-edited via the CLI only."""

    def __init__(self, path: str | os.PathLike[str]):
        self._store = JsonDictStore(path, "connection registry", sort_keys=True)
        self.path = self._store.path

    def _load(self) -> dict[str, dict]:
        data = self._store.load()
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, dict)}

    def _to_conn(self, name: str, rec: dict) -> Connection:
        return Connection(
            name=name,
            command=str(rec.get("command", "")),
            args=[str(a) for a in rec.get("args", [])],
            env={str(k): str(v) for k, v in (rec.get("env") or {}).items()},
            allowed_tools=[str(t) for t in rec.get("allowed_tools", [])],
            enabled=bool(rec.get("enabled", True)),
        )

    def list(self) -> list[Connection]:
        data = self._load()
        return [self._to_conn(n, data[n]) for n in sorted(data)]

    def get(self, name: str) -> Connection | None:
        data = self._load()
        return self._to_conn(name, data[name]) if name in data else None

    def add(self, name: str, command: str, *, args=(), env=None, allowed_tools=(), enabled: bool = True) -> Connection:
        if not valid_name(name):
            raise ValueError(
                f"bad connection name {name!r}: use lowercase letters, digits, - or _ (max 32 chars)"
            )
        if not command:
            raise ValueError("a connection needs a --command")
        with self._store.locked():
            data = self._load()
            if name in data:
                raise ValueError(f"connection {name!r} already exists")
            data[name] = {
                "command": command,
                "args": list(args),
                "env": dict(env or {}),
                "allowed_tools": list(allowed_tools),
                "enabled": bool(enabled),
            }
            conn = self._to_conn(name, data[name])
            self._store.save(data)
        return conn

    def remove(self, name: str) -> bool:
        with self._store.locked():
            data = self._load()
            if name not in data:
                return False
            del data[name]
            self._store.save(data)
        return True

    def set_enabled(self, name: str, enabled: bool) -> Connection:
        with self._store.locked():
            data = self._load()
            if name not in data:
                raise ValueError(f"no connection named {name!r}")
            data[name]["enabled"] = bool(enabled)
            conn = self._to_conn(name, data[name])
            self._store.save(data)
        return conn

    def to_mcp_config(self) -> dict:
        servers = {
            c.name: {"command": c.command, "args": c.args, "env": c.env}
            for c in self.list()
            if c.enabled
        }
        return {"mcpServers": servers}

    def allowed_tools_for_enabled(self) -> list[str]:
        tools: set[str] = set()
        for c in self.list():
            if c.enabled:
                tools.update(c.allowed_tools)
        return sorted(tools)
