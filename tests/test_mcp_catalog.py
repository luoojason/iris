"""Tests for the curated MCP catalog and one-command install.

A real ConnectionStore over a tmp file is used; prompts are faked so no stdin
is touched and no real MCP server is launched.
"""

from __future__ import annotations

import pytest

from iris.connections import ConnectionStore
from iris.mcp_catalog import CATALOG, CatalogEntry, find, install, render_catalog


def store(tmp_path):
    return ConnectionStore(str(tmp_path / "conns.json"))


def fake_prompt(answers):
    """Return a prompt_fn that answers each call from a queue, recording calls."""
    calls = []

    def prompt_fn(text, secret):
        calls.append((text, secret))
        return answers.pop(0)

    prompt_fn.calls = calls
    return prompt_fn


def test_catalog_is_frozen_and_valid():
    assert isinstance(CATALOG, tuple) and len(CATALOG) == 3
    names = {entry.name for entry in CATALOG}
    assert names == {"playwright", "filesystem", "fetch"}
    for entry in CATALOG:
        assert isinstance(entry, CatalogEntry)
        # frozen dataclass: attributes cannot be reassigned
        with pytest.raises(Exception):
            entry.name = "mutated"


def test_render_catalog_lists_names_and_descriptions():
    text = render_catalog()
    for entry in CATALOG:
        assert entry.name in text
        assert entry.description in text


def test_find_known_and_unknown():
    assert find("playwright") is not None
    assert find("nope") is None


def test_install_adds_connection_with_command_and_env(tmp_path):
    s = store(tmp_path)
    prompt_fn = fake_prompt(["https://gw.example", "secret-token"])

    msg = install("fetch", s, prompt_fn)

    assert "installed 'fetch'" in msg
    conn = s.get("fetch")
    assert conn is not None
    assert conn.command == "uvx"
    assert conn.args == ["mcp-server-fetch"]
    assert conn.env == {"FETCH_BASE_URL": "https://gw.example", "FETCH_API_TOKEN": "secret-token"}
    assert conn.allowed_tools == ["mcp__fetch__fetch"]
    # the secret flag is forwarded to the prompt callback
    assert prompt_fn.calls == [
        ("Base URL of the fetch gateway", False),
        ("Bearer token for the fetch gateway", True),
    ]


def test_install_no_env_server(tmp_path):
    s = store(tmp_path)
    prompt_fn = fake_prompt([])

    install("playwright", s, prompt_fn)

    conn = s.get("playwright")
    assert conn is not None
    assert conn.command == "npx" and conn.args == ["@playwright/mcp@latest"]
    assert conn.env == {}
    assert prompt_fn.calls == []  # nothing to prompt for


def test_install_refuses_duplicate_then_force_replaces(tmp_path):
    s = store(tmp_path)
    s.add("playwright", "old-command")

    msg = install("playwright", s, fake_prompt([]))
    assert "already exists" in msg
    assert s.get("playwright").command == "old-command"  # untouched

    msg = install("playwright", s, fake_prompt([]), force=True)
    assert "installed 'playwright'" in msg
    assert s.get("playwright").command == "npx"  # replaced


def test_install_unknown_name(tmp_path):
    s = store(tmp_path)
    msg = install("does-not-exist", s, fake_prompt([]))
    assert "unknown MCP server" in msg
    assert s.get("does-not-exist") is None
