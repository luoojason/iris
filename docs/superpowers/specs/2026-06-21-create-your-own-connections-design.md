# Create Your Own Connections — Design

**Date:** 2026-06-21
**Status:** Approved-on-auto, for owner review
**Base:** `origin/main` (canonical line)
**Parent vision:** `2026-06-21-comprehensive-autonomous-iris-design.md` (sub-project S1)

## Goal

Make connecting and creating your own MCP servers a first-class, easy capability, so Iris's tools are entirely owner-chosen and **nothing is pre-set**. Replace today's "hand-edit a JSON file, set env vars, restart" flow with a managed `iris mcp` CLI, and reframe the built-in Iris servers as optional connections the owner opts into.

## Principle

Iris core auto-loads no integrations. The built-in servers (memory, reminders, jobs, usage, wiki, goals, history, skills, discord, tts, youtube, approvals) are *optional connections* the owner can enable, exactly like any third-party MCP server. Connection config is written only by the owner via the CLI; the model never writes it (same security model as workspaces). The allowlist stays the execution boundary; `--strict-mcp-config` stays.

## Current state (what exists today, on main)

- `ClaudeDriver` appends `--mcp-config <path> --strict-mcp-config` when `Config.mcp_config` (`IRIS_MCP_CONFIG`) is set, and `--allowedTools <list>` from `Config.allowed_tools` (`IRIS_ALLOWED_TOOLS`).
- `examples/mcp.example.json` ships 9 internal servers as a *sample* file; nothing is auto-loaded.
- Adding a server = edit that JSON, add the server's `mcp__<name>__*` tools to `IRIS_ALLOWED_TOOLS`, restart. No CLI, no validation, no test, no in-chat path.
- `iris/statefile.py` provides `JsonDictStore`/`JsonListStore` atomic stores; `iris/workspaces.py` is the model to mirror (owner-CLI-only writer, slug-validated names).

## Architecture

A new **connection registry** becomes the single source of truth for both the MCP servers and their allowed tools. The driver derives its `--mcp-config` and allowlist from enabled connections, with full backward compatibility.

```
iris mcp <add|list|remove|enable|disable|test|import>   (owner CLI, sole writer)
        |
        v
ConnectionStore (iris/connections.py, on statefile JsonDictStore)
   connections.json: { name: {command, args[], env{}, allowed_tools[], enabled} }
        |
        v
Config / ClaudeDriver derivation:
   enabled connections  ->  generated mcp-config (mcpServers block)
                        ->  allowlist = union(connection.allowed_tools) + IRIS_ALLOWED_TOOLS
```

## Components

### 1. `iris/connections.py` — `ConnectionStore`
Built on `statefile.JsonDictStore` (atomic, locked). One record per connection:
`{name, command, args: list[str], env: dict[str,str], allowed_tools: list[str], enabled: bool}`.
Operations: `list()`, `get(name)`, `add(name, command, args, env, allowed_tools, enabled=True)`, `remove(name)`, `set_enabled(name, bool)`, `to_mcp_config() -> dict` (the `{"mcpServers": {...}}` block for enabled connections), `allowed_tools_for_enabled() -> list[str]`. Names are slug-validated (lowercase, `[a-z0-9_-]`), matching the `mcp__<name>__` prefix convention. Path from `IRIS_CONNECTIONS_FILE` (default `iris-connections.json`).

### 2. `iris mcp` CLI (in `iris/cli.py`)
- `iris mcp add NAME --command CMD [--arg A]... [--env K=V]... [--allow TOOL]... [--allow-all]` — register a connection. `--allow` names specific `mcp__NAME__tool` tools; `--allow-all` allows the whole `mcp__NAME__*` prefix.
- `iris mcp list [--json]` — name, command, enabled, allowed tools.
- `iris mcp remove NAME`
- `iris mcp enable NAME` / `iris mcp disable NAME`
- `iris mcp test NAME` — spawn the server through a probe and list the tools it exposes, so the owner can see what to `--allow`. Reports failure clearly; does not require the tools be allowlisted to probe.
- `iris mcp import PATH` — read an existing `mcp.json` (e.g. `examples/mcp.example.json` or a third-party config) and register each `mcpServers` entry as a connection (disabled by default until the owner enables + allows tools).

### 3. Driver derivation (in `iris/config.py` / `iris/driver.py`)
At startup, if a connections file exists and has entries, the driver builds a generated mcp-config from enabled connections and sets the allowlist to `union(connection.allowed_tools) + IRIS_ALLOWED_TOOLS`. If no connections file exists, fall back to today's behavior (`IRIS_MCP_CONFIG` + `IRIS_ALLOWED_TOOLS`) so existing setups keep working unchanged. `--strict-mcp-config` is always preserved.

### 4. `doctor` integration
Extend `iris doctor` to list connections, flag any with a non-resolvable `command`, and flag enabled connections whose `allowed_tools` is empty (they would be silently inert).

### 5. De-hard-code + reframe (ties to S2)
- The built-in servers are documented as optional connections importable via `iris mcp import`; none auto-load.
- Native publishers (`social.py`, and the fork's `buffer.py`) are removed from core in S2; publishing becomes a user-added connection. This spec does not delete them but stops treating any integration as pre-set.

## Data flow

Owner runs `iris mcp add` / `import` / `enable` → `ConnectionStore` writes `iris-connections.json` (atomic) → next start, the driver derives the generated mcp-config + allowlist from enabled connections → the agent has exactly those tools. The model can read the connection list (for "what can I do?") but cannot write it.

## Error handling

Invalid/duplicate names (slug-validated, dup rejected); missing `--command`; malformed `--env` (must be `K=V`); `test` spawn failure reported with stderr tail; atomic writes so a crash never corrupts the registry; enabling a connection with no allowed tools warns (inert); back-compat fallback when no connections file.

## Security

Config writes are owner-CLI only; the model never edits connections (mirrors workspaces). `--strict-mcp-config` preserved so only owner-registered servers load. Allowlist remains the boundary — a registered-but-unallowed tool never executes. `test` spawns the server in a probe, not the live agent. Secrets in `env` live in the connections file (file-mode 600, like other state) and are never echoed by `list`.

## Testing

All offline (no real MCP servers; a fake command/probe):
- `ConnectionStore`: add/list/remove/enable/disable, slug validation, duplicate rejection, atomic write, `to_mcp_config()` shape, `allowed_tools_for_enabled()` union, secret redaction in `list`.
- Driver derivation: connections.json → generated mcp-config + allowlist; back-compat fallback to `IRIS_MCP_CONFIG`/`IRIS_ALLOWED_TOOLS` when no file; `--strict-mcp-config` always present.
- CLI: each subcommand, env/arg parsing, `import` of a sample config, `test` success/failure formatting.
- `doctor`: unresolvable command + empty-allowlist warnings.

## Out of scope (v1)

GUI; an MCP marketplace/auto-discovery; multi-tenant; approval-gated **in-chat** connection adds (a strong follow-up: model proposes a connection spec → Discord Approve button → the bot, not the model, writes it via `ConnectionStore` — reuses the existing approvals system); remote/HTTP MCP auth flows beyond passing env vars.

## Open item

Confirm the connections-file-as-source-of-truth model (driver derives mcp-config + allowlist) versus keeping `IRIS_MCP_CONFIG` primary and only layering the CLI on top. The spec chooses the former for a true first-class "connections" unit, with back-compat fallback to the latter.
