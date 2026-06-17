"""Tests for the skills MCP server (propose_skill / list_skill_proposals)."""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")  # the server needs the MCP SDK; skip if absent

from iris.skills import SkillProposalStore
from iris.mcp import skills as srv

_GOOD = "---\nname: summarize\ndescription: Summarize a doc into bullets\n---\nBody."


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "STORE", SkillProposalStore(tmp_path / "p.json"))
    monkeypatch.setattr(srv, "MAX_PENDING", 10)
    return srv


def test_propose_skill_stages_without_applying(server):
    out = server.propose_skill("summarize", _GOOD, "I'm often asked to summarize")
    assert "#1" in out and "approve" in out.lower()  # tells the owner how to apply
    p = server.STORE.all()[0]
    assert p["status"] == "pending"  # staged, NOT live


def test_propose_skill_rejects_an_invalid_skill(server):
    out = server.propose_skill("Bad Name", _GOOD, "r")
    assert "slug" in out.lower() or "name" in out.lower()
    assert server.STORE.all() == []  # nothing staged


def test_propose_skill_rejects_missing_description(server):
    out = server.propose_skill("ok", "just a body, no frontmatter", "r")
    assert "description" in out.lower()
    assert server.STORE.all() == []


def test_list_skill_proposals(server):
    server.propose_skill("summarize", _GOOD, "rationale text")
    out = server.list_skill_proposals()
    assert "#1" in out and "summarize" in out


def test_pending_cap_blocks_runaway_proposals(server, monkeypatch):
    monkeypatch.setattr(srv, "MAX_PENDING", 1)
    server.propose_skill("summarize", _GOOD, "r")
    out = server.propose_skill("another", _GOOD.replace("summarize", "another"), "r")
    assert "approve" in out.lower() or "pending" in out.lower()
    assert len(server.STORE.pending()) == 1


def test_max_pending_survives_a_non_numeric_env(monkeypatch):
    monkeypatch.setattr(srv, "MAX_PENDING", None)
    monkeypatch.setenv("IRIS_SKILL_PROPOSALS_MAX", "10x")  # garbage
    assert srv._max_pending() == 10  # falls back to the default, does not raise
