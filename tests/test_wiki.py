"""Tests for the wiki MCP tools (iris/mcp/wiki.py)."""

from __future__ import annotations

import os

import pytest

import iris.mcp.wiki as srv
from iris.config import Config


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    monkeypatch.setattr(srv, "_CONFIG", Config(wiki_dir=str(root)))
    return root


def test_unconfigured_wiki_says_so(monkeypatch):
    monkeypatch.setattr(srv, "_CONFIG", Config(wiki_dir=""))
    for reply in (
        srv.wiki_list(),
        srv.wiki_read("Projects/Iris"),
        srv.wiki_search("x"),
        srv.wiki_write("a", "b"),
        srv.wiki_append("a", "b"),
    ):
        assert "not configured" in reply


def test_write_then_read_roundtrip(vault):
    reply = srv.wiki_write("Projects/Iris", "# Iris\n\nthe agent")
    assert "Projects/Iris" in reply
    assert (vault / "Projects" / "Iris.md").read_text("utf-8") == "# Iris\n\nthe agent"
    assert srv.wiki_read("Projects/Iris") == "# Iris\n\nthe agent"
    # the explicit .md form names the same page
    assert srv.wiki_read("Projects/Iris.md") == "# Iris\n\nthe agent"


def test_append_creates_and_separates(vault):
    srv.wiki_append("log", "first entry")
    srv.wiki_append("log", "second entry")
    text = (vault / "log.md").read_text("utf-8")
    assert "first entry\n\nsecond entry" in text


def test_read_missing_page(vault):
    assert "no page" in srv.wiki_read("nope").lower()


def test_list_and_prefix(vault):
    srv.wiki_write("Projects/Iris", "x")
    srv.wiki_write("Projects/GeoAI", "x")
    srv.wiki_write("index", "x")
    everything = srv.wiki_list()
    assert "Projects/Iris" in everything and "index" in everything
    assert ".md" not in everything  # names, not filenames
    projects = srv.wiki_list(prefix="Projects/")
    assert "GeoAI" in projects and "index" not in projects


def test_search_is_case_insensitive(vault):
    srv.wiki_write("Projects/Iris", "The Job Coordinator design\nother line")
    hits = srv.wiki_search("job coordinator")
    assert "Projects/Iris" in hits
    assert "Job Coordinator design" in hits
    assert "no pages match" in srv.wiki_search("zzz-nothing").lower()


def test_bad_names_are_rejected(vault):
    for bad in ("/etc/passwd", "../escape", "a/../../b", "", "notes.txt", "a\\b", "a\0b"):
        for reply in (srv.wiki_read(bad), srv.wiki_write(bad, "x"), srv.wiki_append(bad, "x")):
            assert "page name" in reply.lower() or "not configured" in reply.lower(), (bad, reply)
    # nothing was created by any of those
    assert srv.wiki_list() == "The wiki is empty."


def test_symlinked_page_cannot_escape_the_vault(vault, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("hidden", encoding="utf-8")
    (vault / "leak.md").symlink_to(secret)
    reply = srv.wiki_read("leak")
    assert "hidden" not in reply
    assert "page name" in reply.lower() or "outside" in reply.lower()


def test_read_is_capped(vault, monkeypatch):
    monkeypatch.setattr(srv, "READ_CAP", 10)
    srv.wiki_write("big", "0123456789ABCDEF")
    reply = srv.wiki_read("big")
    assert reply.startswith("0123456789")
    assert "truncated" in reply


def test_list_is_capped(vault, monkeypatch):
    monkeypatch.setattr(srv, "LIST_CAP", 2)
    for n in range(4):
        srv.wiki_write(f"page-{n}", "x")
    reply = srv.wiki_list()
    assert "and 2 more" in reply


def test_outputs_never_contain_the_vault_path(vault):
    srv.wiki_write("Projects/Iris", "content")
    for reply in (srv.wiki_list(), srv.wiki_read("Projects/Iris"),
                  srv.wiki_search("content"), srv.wiki_write("x", "y"),
                  srv.wiki_append("x", "z"), srv.wiki_read("missing")):
        assert str(vault) not in reply


def test_wiki_dir_config_knob(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IRIS_WIKI_DIR", "/some/vault")
    cfg = Config.from_env(dotenv=tmp_path / "none.env")
    assert cfg.wiki_dir == "/some/vault"
    monkeypatch.delenv("IRIS_WIKI_DIR")
    assert Config.from_env(dotenv=tmp_path / "none.env").wiki_dir == ""
