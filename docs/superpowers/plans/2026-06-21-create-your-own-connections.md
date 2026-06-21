# Create Your Own Connections — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make connecting your own MCP servers a first-class capability via an `iris mcp` CLI backed by a connections registry, with the driver deriving its `--mcp-config` and allowlist from enabled connections (back-compatible with today's env-based config).

**Architecture:** A new `ConnectionStore` (on the existing `statefile.JsonDictStore`) owns `iris-connections.json`. `iris mcp <add|list|remove|enable|disable|import|test>` is the sole writer (owner CLI; the model never writes it). At runtime `Agent.from_config` resolves enabled connections into a generated mcp-config file plus a derived allowlist; if no connections file exists, behavior is unchanged.

**Tech Stack:** Python 3.10+, `iris/statefile.py` (atomic locked JSON stores), argparse CLI in `iris/cli.py`, `claude -p --mcp-config --strict-mcp-config --allowedTools` (unchanged driver interface).

## Global Constraints

- Python >= 3.10; `from __future__ import annotations` at the top of every module.
- The model never writes connection config; only the owner CLI writes it (mirror `iris/workspaces.py`).
- `--strict-mcp-config` stays on whenever an mcp-config is passed (unchanged in `iris/driver.py`).
- The allowlist remains the execution boundary; a registered-but-unallowed tool must never run.
- Connection names are slug-validated: `^[a-z0-9][a-z0-9_-]{0,31}$` (same shape as workspaces).
- Back-compat: when no connections file exists, `IRIS_MCP_CONFIG` / `IRIS_ALLOWED_TOOLS` behavior is unchanged.
- Secrets in a connection's `env` are stored in the connections file and never printed by `list`.
- Connections file path comes from `IRIS_CONNECTIONS_FILE` (default `iris-connections.json`).
- Commit messages: plain imperative, no AI-authorship markers, no emojis.
- After each task the full suite (`python -m pytest -q`) passes.

## File Structure

- `iris/connections.py` (create) — `Connection` dataclass, `valid_name`, `ConnectionStore`, `resolve_connections`.
- `iris/config.py` (modify) — add `connections_file` field + `IRIS_CONNECTIONS_FILE` env read.
- `iris/agent.py` (modify) — `from_config` resolves connections before building the driver.
- `iris/cli.py` (modify) — `iris mcp` subcommand tree + dispatch; doctor connections section.
- `iris/mcp_probe.py` (create) — minimal MCP stdio probe used by `iris mcp test`.
- `tests/test_connections.py` (create) — store + resolve tests.
- `tests/test_mcp_cli.py` (create) — CLI tests.
- `tests/test_mcp_probe.py` (create) — probe tests.
- `README.md`, `.env.example` (modify) — document the capability; reframe built-in servers as optional.

---

### Task 1: ConnectionStore registry

**Files:**
- Create: `iris/connections.py`
- Test: `tests/test_connections.py`

**Interfaces:**
- Produces:
  - `valid_name(name: str) -> bool`
  - `@dataclass Connection(name: str, command: str, args: list[str], env: dict[str,str], allowed_tools: list[str], enabled: bool)`
  - `class ConnectionStore(path)`: `list() -> list[Connection]` (sorted by name); `get(name) -> Connection | None`; `add(name, command, *, args=(), env=None, allowed_tools=(), enabled=True) -> Connection` (raises `ValueError` on bad name, duplicate, or empty command); `remove(name) -> bool`; `set_enabled(name, enabled) -> Connection` (raises `ValueError` if missing); `to_mcp_config() -> dict` (`{"mcpServers": {name: {"command","args","env"}}}` for enabled only); `allowed_tools_for_enabled() -> list[str]` (deduped, sorted union).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_connections.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/Desktop/iris && python -m pytest tests/test_connections.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'iris.connections'`).

- [ ] **Step 3: Write the minimal implementation**

Create `iris/connections.py`:

```python
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
            args=[str(a) for a in rec.get("args", []) if isinstance(a, str)],
            env={str(k): str(v) for k, v in (rec.get("env") or {}).items()},
            allowed_tools=[str(t) for t in rec.get("allowed_tools", []) if isinstance(t, str)],
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
            self._store.save(data)
        return self.get(name)

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
            self._store.save(data)
        return self.get(name)

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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/Desktop/iris && python -m pytest tests/test_connections.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/iris
git add iris/connections.py tests/test_connections.py
git commit -m "Add ConnectionStore registry for owner MCP connections"
```

---

### Task 2: Driver derivation (materialize + resolve + wire)

**Files:**
- Modify: `iris/connections.py`
- Modify: `iris/config.py` (add `connections_file` field + `IRIS_CONNECTIONS_FILE`)
- Modify: `iris/agent.py` (`from_config` resolves connections)
- Test: `tests/test_connections.py` (append)

**Interfaces:**
- Consumes: `ConnectionStore`, `Connection` (Task 1); `iris.config.Config` (`mcp_config`, `allowed_tools`, `connections_file`).
- Produces:
  - `ConnectionStore.materialize(dest: str) -> str | None` — write `to_mcp_config()` JSON to `dest`; return `dest` if any server is enabled, else `None` (and do not write).
  - `resolve_connections(config, *, generated_path=None) -> Config` — if `config.connections_file` exists and has enabled connections, materialize a generated mcp-config (default `<connections_file>.generated.json`) and return `dataclasses.replace(config, mcp_config=<generated>, allowed_tools=sorted(set(union + config.allowed_tools)))`; otherwise return `config` unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_connections.py`:

```python
import json
from dataclasses import dataclass, field

from iris.connections import resolve_connections


@dataclass
class FakeConfig:
    connections_file: str = "iris-connections.json"
    mcp_config: str | None = None
    allowed_tools: list = field(default_factory=list)


def test_materialize_writes_enabled_only(tmp_path):
    s = store(tmp_path)
    s.add("a", "cmda", allowed_tools=["mcp__a__x"])
    s.add("b", "cmdb", enabled=False)
    dest = str(tmp_path / "gen.json")
    out = s.materialize(dest)
    assert out == dest
    data = json.loads(open(dest).read())
    assert list(data["mcpServers"]) == ["a"]


def test_materialize_none_when_no_enabled(tmp_path):
    s = store(tmp_path)
    s.add("b", "cmdb", enabled=False)
    dest = str(tmp_path / "gen.json")
    assert s.materialize(dest) is None
    assert not (tmp_path / "gen.json").exists()


def test_resolve_connections_derives_config(tmp_path):
    cfile = tmp_path / "conns.json"
    s = ConnectionStore(str(cfile))
    s.add("a", "cmda", allowed_tools=["mcp__a__x", "mcp__a__y"])
    cfg = FakeConfig(connections_file=str(cfile), allowed_tools=["mcp__keep__z"])
    out = resolve_connections(cfg, generated_path=str(tmp_path / "gen.json"))
    assert out.mcp_config == str(tmp_path / "gen.json")
    assert out.allowed_tools == ["mcp__a__x", "mcp__a__y", "mcp__keep__z"]


def test_resolve_connections_passthrough_when_no_file(tmp_path):
    cfg = FakeConfig(connections_file=str(tmp_path / "missing.json"), mcp_config="orig.json", allowed_tools=["t"])
    out = resolve_connections(cfg)
    assert out is cfg  # unchanged object, back-compat
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/Desktop/iris && python -m pytest tests/test_connections.py -k "materialize or resolve" -q`
Expected: FAIL (`AttributeError: 'ConnectionStore' object has no attribute 'materialize'` / `ImportError: cannot import name 'resolve_connections'`).

- [ ] **Step 3: Write the minimal implementation**

Add to `iris/connections.py` (imports `json`, `replace` already imported):

```python
import json


# (add inside ConnectionStore)
    def materialize(self, dest: str) -> str | None:
        cfg = self.to_mcp_config()
        if not cfg["mcpServers"]:
            return None
        path = Path(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        return dest


def resolve_connections(config, *, generated_path: str | None = None):
    """Derive mcp_config + allowed_tools from enabled connections.

    Back-compat: if no connections file exists (or none are enabled), the config
    is returned unchanged so IRIS_MCP_CONFIG / IRIS_ALLOWED_TOOLS keep working.
    """
    cfile = getattr(config, "connections_file", None)
    if not cfile or not Path(cfile).exists():
        return config
    store = ConnectionStore(cfile)
    gen = generated_path or str(Path(cfile).with_suffix(".generated.json"))
    mcp_path = store.materialize(gen)
    if mcp_path is None:
        return config
    merged = sorted(set(store.allowed_tools_for_enabled()) | set(config.allowed_tools or []))
    return replace(config, mcp_config=mcp_path, allowed_tools=merged)
```

Add `import json` near the top of the module (with the other stdlib imports) and keep the in-method version out (use the module-level import). Ensure `from dataclasses import ... replace` is present (it is).

In `iris/config.py`, add the field to the `Config` dataclass (near `mcp_config`):

```python
    connections_file: str = "iris-connections.json"
```

and in `Config.from_env(...)` add (near the `mcp_config=` line):

```python
            connections_file=os.environ.get("IRIS_CONNECTIONS_FILE", "iris-connections.json"),
```

In `iris/agent.py`, in `from_config`, resolve before reading mcp/allowlist fields. At the very top of `from_config` body:

```python
        from .connections import resolve_connections
        config = resolve_connections(config)
```

(Everything below that builds `ClaudeDriver(mcp_config=config.mcp_config, allowed_tools=config.allowed_tools or None, ...)` now sees the derived values.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/Desktop/iris && python -m pytest tests/test_connections.py -q && python -m pytest tests/test_config.py tests/test_agent.py -q`
Expected: PASS (connections file green; config/agent suites still green).

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/iris
git add iris/connections.py iris/config.py iris/agent.py tests/test_connections.py
git commit -m "Derive mcp-config and allowlist from enabled connections"
```

---

### Task 3: `iris mcp` CLI (add/list/remove/enable/disable/import)

**Files:**
- Modify: `iris/cli.py`
- Test: `tests/test_mcp_cli.py` (create)

**Interfaces:**
- Consumes: `ConnectionStore` (Task 1), `Config.connections_file` (Task 2).
- Produces: an `iris mcp` argparse subtree dispatched in `main`. Subcommands: `add NAME --command CMD [--arg A]... [--env K=V]... [--allow TOOL]... [--allow-all]`, `list [--json]`, `remove NAME`, `enable NAME`, `disable NAME`, `import PATH`. A helper `mcp_command(args, config) -> int` does the work and returns an exit code.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_cli.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/Desktop/iris && python -m pytest tests/test_mcp_cli.py -q`
Expected: FAIL (`ImportError: cannot import name 'mcp_command'`).

- [ ] **Step 3: Write the minimal implementation**

In `iris/cli.py`, add the helper (near the other command helpers):

```python
def _parse_kv(pairs):
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"bad --env {p!r}: expected K=V")
        k, _, v = p.partition("=")
        out[k.strip()] = v
    return out


def mcp_command(args, config) -> int:
    """Owner CLI for MCP connections. Returns a process exit code."""
    from .connections import ConnectionStore

    store = ConnectionStore(config.connections_file)
    action = args.mcp_action

    if action == "add":
        allow = list(args.allow or [])
        if args.allow_all:
            allow.append(f"mcp__{args.name}")
        try:
            store.add(
                args.name, args.command,
                args=args.arg or [], env=_parse_kv(args.env),
                allowed_tools=allow,
            )
        except ValueError as exc:
            print(f"error: {exc}")
            return 1
        print(f"added connection {args.name!r} (enabled). Allowed tools: {allow or '(none yet — add with --allow)'}")
        return 0

    if action == "list":
        conns = store.list()
        if args.json:
            import json as _json
            print(_json.dumps([
                {"name": c.name, "command": c.command, "args": c.args,
                 "enabled": c.enabled, "allowed_tools": c.allowed_tools,
                 "env_keys": sorted(c.env)} for c in conns
            ], indent=2))
            return 0
        if not conns:
            print("no connections. Add one with: iris mcp add NAME --command CMD")
            return 0
        for c in conns:
            state = "on " if c.enabled else "off"
            tools = ", ".join(c.allowed_tools) or "(no tools allowed)"
            print(f"[{state}] {c.name}: {c.command} {' '.join(c.args)}  ->  {tools}")
        return 0

    if action == "remove":
        ok = store.remove(args.name)
        print(f"removed {args.name!r}" if ok else f"no connection named {args.name!r}")
        return 0 if ok else 1

    if action in ("enable", "disable"):
        try:
            store.set_enabled(args.name, action == "enable")
        except ValueError as exc:
            print(f"error: {exc}")
            return 1
        print(f"{action}d {args.name!r}")
        return 0

    if action == "import":
        import json as _json
        try:
            data = _json.loads(open(args.path, encoding="utf-8").read())
        except (OSError, ValueError) as exc:
            print(f"error: cannot read {args.path}: {exc}")
            return 1
        servers = (data or {}).get("mcpServers", {})
        if not servers:
            print("no mcpServers found in that file")
            return 1
        added = 0
        for name, spec in servers.items():
            if store.get(name) is not None:
                print(f"skip {name!r}: already exists")
                continue
            try:
                store.add(
                    name, str(spec.get("command", "")),
                    args=[str(a) for a in spec.get("args", [])],
                    env={str(k): str(v) for k, v in (spec.get("env") or {}).items()},
                    allowed_tools=[], enabled=False,
                )
                added += 1
            except ValueError as exc:
                print(f"skip {name!r}: {exc}")
        print(f"imported {added} connection(s), disabled. Enable + allow tools, e.g.: iris mcp enable NAME")
        return 0

    print("unknown mcp action")
    return 1
```

In `build_parser()` (the function that builds the argparse tree — find where subparsers like `workspaces` are added), add the `mcp` subtree:

```python
    mcp_p = sub.add_parser("mcp", help="connect your own MCP servers")
    mcp_sub = mcp_p.add_subparsers(dest="mcp_action", required=True)

    p_add = mcp_sub.add_parser("add", help="register an MCP server connection")
    p_add.add_argument("name")
    p_add.add_argument("--command", required=True)
    p_add.add_argument("--arg", action="append", help="a command argument (repeatable)")
    p_add.add_argument("--env", action="append", help="K=V env var (repeatable)")
    p_add.add_argument("--allow", action="append", help="an allowed tool name (repeatable)")
    p_add.add_argument("--allow-all", action="store_true", help="allow the whole server (mcp__NAME)")

    p_list = mcp_sub.add_parser("list", help="list connections")
    p_list.add_argument("--json", action="store_true")

    for verb in ("remove", "enable", "disable"):
        pv = mcp_sub.add_parser(verb, help=f"{verb} a connection")
        pv.add_argument("name")

    p_imp = mcp_sub.add_parser("import", help="import servers from an existing mcp.json")
    p_imp.add_argument("path")
```

In `main(...)` dispatch, add (near the `workspaces` dispatch):

```python
    if command == "mcp":
        return mcp_command(args, config)
```

> **Implementer note:** the test imports `build_parser` from `iris.cli`. If the parser is currently built inline inside `main`, extract it into a module-level `build_parser() -> argparse.ArgumentParser` that `main` calls, so the tests can parse argv without running commands. Keep `main`'s behavior identical.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/Desktop/iris && python -m pytest tests/test_mcp_cli.py -q && python -m pytest tests/test_cli.py -q`
Expected: PASS (mcp CLI green; existing CLI tests still green).

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/iris
git add iris/cli.py tests/test_mcp_cli.py
git commit -m "Add `iris mcp` CLI for managing connections"
```

---

### Task 4: `iris mcp test` — minimal MCP stdio probe

**Files:**
- Create: `iris/mcp_probe.py`
- Modify: `iris/cli.py` (add the `test` subcommand + dispatch in `mcp_command`)
- Test: `tests/test_mcp_probe.py` (create)

**Interfaces:**
- Consumes: `ConnectionStore` (Task 1).
- Produces: `probe_tools(command: str, args: list[str], env: dict[str,str], *, timeout: float = 10.0, spawn=None) -> list[str]` in `iris/mcp_probe.py` — speaks the minimal MCP handshake (`initialize` then `tools/list`) over the child's stdio and returns the tool names; raises `ProbeError` on failure/timeout. `spawn` is injectable for tests (default `subprocess.Popen`). Plus `class ProbeError(RuntimeError)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mcp_probe.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/Desktop/iris && python -m pytest tests/test_mcp_probe.py -q`
Expected: FAIL (`ModuleNotFoundError: No module named 'iris.mcp_probe'`).

- [ ] **Step 3: Write the minimal implementation**

Create `iris/mcp_probe.py`:

```python
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
```

In `iris/cli.py` `mcp_command`, add the `test` action (before the final `unknown` fallback):

```python
    if action == "test":
        conn = store.get(args.name)
        if conn is None:
            print(f"no connection named {args.name!r}")
            return 1
        from .mcp_probe import ProbeError, probe_tools
        try:
            tools = probe_tools(conn.command, conn.args, conn.env)
        except ProbeError as exc:
            print(f"could not probe {args.name!r}: {exc}")
            return 1
        if not tools:
            print(f"{args.name!r} started but exposed no tools")
            return 0
        print(f"{args.name!r} exposes: " + ", ".join(f"mcp__{args.name}__{t}" for t in tools))
        print("allow them with: iris mcp add (or re-add) using --allow <tool>")
        return 0
```

In `build_parser()`, add the `test` subcommand under the `mcp` subtree:

```python
    p_test = mcp_sub.add_parser("test", help="probe a connection and list its tools")
    p_test.add_argument("name")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/Desktop/iris && python -m pytest tests/test_mcp_probe.py tests/test_mcp_cli.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/iris
git add iris/mcp_probe.py iris/cli.py tests/test_mcp_probe.py
git commit -m "Add `iris mcp test` MCP stdio probe"
```

---

### Task 5: doctor checks + docs + reframe built-in servers

**Files:**
- Modify: `iris/cli.py` (doctor connections section)
- Modify: `README.md`
- Modify: `.env.example`
- Test: `tests/test_mcp_cli.py` (append a doctor-helper test)

**Interfaces:**
- Consumes: `ConnectionStore` (Task 1), `Config.connections_file`.
- Produces: `connection_doctor_lines(config) -> list[str]` in `iris/cli.py` — one line per connection plus a warning line for each enabled connection whose `command` is not resolvable on PATH or whose `allowed_tools` is empty. `doctor()` prints these lines.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_cli.py`:

```python
import shutil

from iris.cli import connection_doctor_lines


def test_connection_doctor_flags_empty_allow_and_bad_command(tmp_path):
    c = cfg(tmp_path)
    s = ConnectionStore(c.connections_file)
    s.add("good", shutil.which("python") or "python", allowed_tools=["mcp__good__x"])
    s.add("noallow", shutil.which("python") or "python")  # enabled, no tools
    s.add("badcmd", "definitely-not-a-real-binary-xyz", allowed_tools=["mcp__badcmd__y"])
    lines = "\n".join(connection_doctor_lines(c))
    assert "good" in lines
    assert "noallow" in lines and "no allowed tools" in lines.lower()
    assert "badcmd" in lines and "not found" in lines.lower()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd ~/Desktop/iris && python -m pytest tests/test_mcp_cli.py -k doctor -q`
Expected: FAIL (`ImportError: cannot import name 'connection_doctor_lines'`).

- [ ] **Step 3: Write the implementation**

In `iris/cli.py` add:

```python
def connection_doctor_lines(config) -> list[str]:
    import shutil as _shutil
    from .connections import ConnectionStore

    store = ConnectionStore(config.connections_file)
    conns = store.list()
    if not conns:
        return ["connections: none registered (iris mcp add NAME --command CMD)"]
    lines = [f"connections: {len(conns)} registered"]
    for c in conns:
        state = "on" if c.enabled else "off"
        lines.append(f"  [{state}] {c.name}: {c.command}")
        if c.enabled and _shutil.which(c.command) is None and not c.command.startswith("/"):
            lines.append(f"    WARNING: command not found on PATH: {c.command}")
        if c.enabled and not c.allowed_tools:
            lines.append(f"    WARNING: {c.name} has no allowed tools, so it is inert")
    return lines
```

In `doctor(...)`, after the existing `print(f"allowed tools: ...")` line, add:

```python
    for line in connection_doctor_lines(config):
        print(line)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd ~/Desktop/iris && python -m pytest tests/test_mcp_cli.py -q`
Expected: PASS.

- [ ] **Step 5: Update docs**

In `README.md`, add a section near the MCP/tools docs:

```markdown
## Connect your own MCP servers

Iris ships with no integrations turned on. You connect the tools you want —
any MCP server — and nothing is pre-set.

    iris mcp add buffer --command npx --arg buffer-mcp --env TOKEN=... --allow mcp__buffer__publish
    iris mcp test buffer      # probe the server and list the tools it exposes
    iris mcp list
    iris mcp disable buffer   # turn a connection off without removing it

The built-in Iris servers (memory, reminders, jobs, usage, wiki, goals,
history, skills, discord, tts) are just connections too — import the sample set
and enable the ones you want:

    iris mcp import examples/mcp.example.json
    iris mcp enable memory

Connections are owner-managed from the CLI only; the agent never edits them.
`iris doctor` lists your connections and flags any that are misconfigured.
```

In `.env.example`, add (near the MCP settings):

```
# Connections registry written by `iris mcp` (your own MCP servers). When this
# file has enabled entries it supersedes IRIS_MCP_CONFIG / IRIS_ALLOWED_TOOLS.
IRIS_CONNECTIONS_FILE=iris-connections.json
```

- [ ] **Step 6: Run the full suite**

Run: `cd ~/Desktop/iris && python -m pytest -q`
Expected: PASS (whole suite green).

- [ ] **Step 7: Commit**

```bash
cd ~/Desktop/iris
git add iris/cli.py tests/test_mcp_cli.py README.md .env.example
git commit -m "Surface connections in doctor and document connect-your-own"
```

---

## Notes for the implementer

- The driver interface is unchanged: connections are resolved into `config.mcp_config` (a generated file) and `config.allowed_tools` inside `Agent.from_config`. Do not change `ClaudeDriver.build_command`.
- Back-compat is a hard requirement: with no connections file, every existing env-based setup must behave exactly as before. The passthrough test in Task 2 guards this.
- `--strict-mcp-config` must remain on the `claude` command whenever an mcp-config is present (it already is in `iris/driver.py`; don't touch it).
- The model must never gain a path to writing the connections file — only the `iris mcp` CLI writes it.
