"""A curated catalog of vetted MCP servers plus one-command install.

The owner registers MCP servers with ``iris mcp add`` by hand-writing a command,
args, env, and allowed-tool list. That is fiddly for well-known servers whose
shape never changes. This module ships a small, frozen ``CATALOG`` of vetted
entries and an ``install`` helper that collects any required env values and
writes the connection through ``ConnectionStore.add`` (the same owner-only
writer path used everywhere else). Nothing here ever runs a server; it only
records a spec the driver later materializes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

# A prompt callback: (prompt_text, is_secret) -> the value the owner typed.
# Injected so callers (and tests) decide how the value is read; this module
# never touches stdin itself.
PromptFn = Callable[[str, bool], str]


@dataclass(frozen=True)
class CatalogEntry:
    """One vetted MCP server: how to launch it and what it needs."""

    name: str
    description: str
    command: str
    args: list[str]
    # Each env requirement is (variable name, owner-facing prompt, is_secret).
    env: tuple[tuple[str, str, bool], ...]
    allowed_tools: list[str]


CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        name="playwright",
        description="Official Playwright MCP: drive a real browser (navigate, snapshot, click).",
        command="npx",
        args=["@playwright/mcp@latest"],
        env=(),
        allowed_tools=[
            "mcp__playwright__browser_navigate",
            "mcp__playwright__browser_snapshot",
            "mcp__playwright__browser_click",
        ],
    ),
    CatalogEntry(
        name="filesystem",
        description="Read-only filesystem access scoped to a single root directory.",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/srv/iris/readonly"],
        env=(),
        allowed_tools=[
            "mcp__filesystem__read_file",
            "mcp__filesystem__list_directory",
            "mcp__filesystem__search_files",
        ],
    ),
    CatalogEntry(
        name="fetch",
        description="Fetch and convert web pages over HTTP for the agent to read.",
        command="uvx",
        args=["mcp-server-fetch"],
        env=(
            ("FETCH_BASE_URL", "Base URL of the fetch gateway", False),
            ("FETCH_API_TOKEN", "Bearer token for the fetch gateway", True),
        ),
        allowed_tools=["mcp__fetch__fetch"],
    ),
)


def find(name: str) -> CatalogEntry | None:
    """Return the catalog entry with this name, or None if there is none."""
    for entry in CATALOG:
        if entry.name == name:
            return entry
    return None


def render_catalog() -> str:
    """Return a readable listing of the catalog (name and description)."""
    lines = ["Available MCP servers (install with: iris mcp install NAME):", ""]
    width = max((len(entry.name) for entry in CATALOG), default=0)
    for entry in CATALOG:
        lines.append(f"  {entry.name.ljust(width)}  {entry.description}")
    return "\n".join(lines)


def install(name: str, store, prompt_fn: PromptFn, *, force: bool = False) -> str:
    """Install a vetted server into the connection store. Returns a status line.

    Looks the entry up by name, collects any declared env values via ``prompt_fn``,
    refuses to clobber an existing connection of the same name unless ``force``,
    then records it with ``ConnectionStore.add``. ``prompt_fn`` is injected so the
    caller controls how values are read (real stdin in the CLI, a fake in tests).
    """
    entry = find(name)
    if entry is None:
        known = ", ".join(entry.name for entry in CATALOG) or "(none)"
        return f"unknown MCP server {name!r}. Known servers: {known}"

    existing = {conn.name for conn in store.list()}
    if name in existing and not force:
        return f"connection {name!r} already exists; re-run with force=True to replace it"

    env: dict[str, str] = {}
    for var_name, prompt_text, secret in entry.env:
        env[var_name] = prompt_fn(prompt_text, secret)

    if name in existing:
        store.remove(name)
    store.add(
        entry.name,
        entry.command,
        args=list(entry.args),
        env=env,
        allowed_tools=list(entry.allowed_tools),
    )

    tools = ", ".join(entry.allowed_tools) or "(none)"
    rendered = " ".join([entry.command, *entry.args]).strip()
    return f"installed {entry.name!r} ({rendered}); allowed tools: {tools}"
