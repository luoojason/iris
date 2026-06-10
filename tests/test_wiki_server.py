"""Wiki MCP server tests: read-only tools over a tmp_path vault.

Every tool runs against a vault under tmp_path via the monkeypatched
``srv.WIKI_DIR`` seam; ``srv._now`` is the module's one clock, monkeypatched
for age rendering. The traversal-guard tests are the load-bearing ones: the
server may be pointed at a directory that lives next to secrets, so ``..``
hops, absolute paths, and symlink escapes must all come back as friendly
refusals that never echo the protected content.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("mcp")  # the server needs the MCP SDK; skip if absent

from iris.mcp import wiki_server as srv


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """A small Obsidian-shaped vault, with a secret file OUTSIDE it."""
    root = tmp_path / "vault"
    (root / "Projects").mkdir(parents=True)
    (root / "Projects" / "Iris.md").write_text(
        "# Iris\nIris is the resident Discord agent.\nStatus: shipping v0.2.\n",
        "utf-8")
    (root / "Projects" / "PlotProof.md").write_text(
        "# PlotProof\nEUDR dossier app.\nThe demo map uses iris-colored pins.\n",
        "utf-8")
    (root / "log.md").write_text("2026-06-09 routine vault backup.\n", "utf-8")
    (tmp_path / "secret.md").write_text("TOP-SECRET token=abc123\n", "utf-8")
    monkeypatch.setattr(srv, "WIKI_DIR", root)
    return root


# --- configuration guards -------------------------------------------------

def test_search_with_unconfigured_dir_names_the_env_var(monkeypatch):
    monkeypatch.setattr(srv, "WIKI_DIR", Path(""))
    out = srv.search_wiki("iris")
    assert "IRIS_WIKI_DIR" in out


def test_read_with_unconfigured_dir_names_the_env_var(monkeypatch):
    monkeypatch.setattr(srv, "WIKI_DIR", Path(""))
    assert "IRIS_WIKI_DIR" in srv.read_wiki_page("Projects/Iris.md")


def test_recent_with_unconfigured_dir_names_the_env_var(monkeypatch):
    monkeypatch.setattr(srv, "WIKI_DIR", Path(""))
    assert "IRIS_WIKI_DIR" in srv.recent_wiki_changes()


def test_missing_vault_directory_is_a_friendly_string(tmp_path, monkeypatch):
    monkeypatch.setattr(srv, "WIKI_DIR", tmp_path / "nope")
    out = srv.search_wiki("iris")
    assert "IRIS_WIKI_DIR" in out
    assert "nope" in out


# --- search_wiki ----------------------------------------------------------

def test_search_needs_a_query(vault):
    out = srv.search_wiki("   ")
    assert "query" in out.lower()


def test_search_is_case_insensitive_and_shows_path_line_snippets(vault):
    out = srv.search_wiki("SHIPPING")
    assert "Projects/Iris.md: Status: shipping v0.2." in out


def test_search_ranks_filename_hits_above_content_hits(vault):
    lines = srv.search_wiki("iris").splitlines()
    assert lines[0].startswith("Projects/Iris.md")
    assert any(l.startswith("Projects/PlotProof.md: ") for l in lines[1:])


def test_search_shows_one_best_line_per_file(vault):
    (vault / "Notes.md").write_text(
        "alpha status here\nstatus shipping both here\nalpha again\n", "utf-8")
    out = srv.search_wiki("status shipping")
    assert "Notes.md: status shipping both here" in out
    assert out.count("Notes.md") == 1
    assert "alpha status here" not in out


def test_search_requires_every_term_to_match(vault):
    out = srv.search_wiki("iris zzznope")
    assert "No wiki pages match" in out


def test_search_with_no_hits_is_a_friendly_string(vault):
    out = srv.search_wiki("qqqzzz")
    assert "No wiki pages match" in out
    assert "qqqzzz" in out


def test_search_clamps_to_max_results(vault):
    for i in range(5):
        (vault / f"zebra-{i}.md").write_text("a zebra appears\n", "utf-8")
    assert len(srv.search_wiki("zebra", max_results=2).splitlines()) == 2
    assert len(srv.search_wiki("zebra", max_results=0).splitlines()) == 1


def test_search_skips_files_over_two_megabytes(vault):
    big = vault / "big.md"
    big.write_text("needle\n" + "x" * (2 * 1024 * 1024), "utf-8")
    (vault / "small.md").write_text("a needle in here\n", "utf-8")
    out = srv.search_wiki("needle")
    assert "small.md" in out
    assert "big.md" not in out


def test_search_skips_hidden_folders(vault):
    hidden = vault / ".obsidian"
    hidden.mkdir()
    (hidden / "workspace.md").write_text("iris workspace state\n", "utf-8")
    assert "workspace" not in srv.search_wiki("iris")


def test_search_never_reads_through_an_escaping_symlink(vault, tmp_path):
    os.symlink(tmp_path / "secret.md", vault / "Leak.md")
    out = srv.search_wiki("abc123")  # only the secret's content contains this
    assert out == "No wiki pages match 'abc123'."
    assert "Leak.md" not in out
    assert "token" not in out


# --- read_wiki_page -------------------------------------------------------

def test_read_returns_the_page_verbatim_when_short(vault):
    out = srv.read_wiki_page("Projects/Iris.md")
    assert out == ("# Iris\nIris is the resident Discord agent.\n"
                   "Status: shipping v0.2.\n")


def test_read_needs_a_path(vault):
    assert "path" in srv.read_wiki_page("   ").lower()


def test_read_of_a_missing_page_is_a_friendly_string(vault):
    out = srv.read_wiki_page("Projects/Nope.md")
    assert "No wiki page" in out
    assert "Projects/Nope.md" in out


def test_read_truncates_with_a_footer_showing_total_size(vault):
    (vault / "Long.md").write_text("y" * 9_000, "utf-8")
    out = srv.read_wiki_page("Long.md")  # default max_chars=8000
    assert out.startswith("y" * 8_000)
    assert "y" * 8_001 not in out
    assert "truncated" in out
    assert "9000 chars total" in out


def test_read_clamps_max_chars_to_a_floor_of_200(vault):
    (vault / "Long.md").write_text("z" * 500, "utf-8")
    out = srv.read_wiki_page("Long.md", max_chars=10)
    assert out.startswith("z" * 200)
    assert "z" * 201 not in out
    assert "truncated" in out


def test_read_caps_max_chars_at_50000(vault):
    (vault / "Huge.md").write_text("w" * 60_000, "utf-8")
    out = srv.read_wiki_page("Huge.md", max_chars=999_999)
    assert out.startswith("w" * 50_000)
    assert "w" * 50_001 not in out
    assert "60000 chars total" in out


def test_read_refuses_dotdot_traversal_without_leaking(vault):
    out = srv.read_wiki_page("../secret.md")
    assert "TOP-SECRET" not in out
    assert "outside" in out.lower()


def test_read_refuses_deep_dotdot_traversal_without_leaking(vault):
    out = srv.read_wiki_page("Projects/../../secret.md")
    assert "TOP-SECRET" not in out
    assert "outside" in out.lower()


def test_read_refuses_absolute_paths_without_leaking(vault, tmp_path):
    out = srv.read_wiki_page(str(tmp_path / "secret.md"))
    assert "TOP-SECRET" not in out
    assert "relative" in out.lower()


def test_read_refuses_a_symlink_that_escapes_the_vault(vault, tmp_path):
    os.symlink(tmp_path / "secret.md", vault / "Leak.md")
    out = srv.read_wiki_page("Leak.md")
    assert "TOP-SECRET" not in out
    assert "outside" in out.lower()


def test_read_refuses_non_markdown_files(vault):
    (vault / "secrets.txt").write_text("INSIDE-SECRET\n", "utf-8")
    out = srv.read_wiki_page("secrets.txt")
    assert "INSIDE-SECRET" not in out
    assert ".md" in out


def test_read_refuses_an_md_symlink_to_a_non_markdown_file(vault):
    (vault / "secrets.txt").write_text("INSIDE-SECRET\n", "utf-8")
    os.symlink(vault / "secrets.txt", vault / "Sneaky.md")
    out = srv.read_wiki_page("Sneaky.md")
    assert "INSIDE-SECRET" not in out


def test_read_allows_dotdot_that_resolves_back_inside(vault):
    out = srv.read_wiki_page("Projects/../Projects/Iris.md")
    assert "Status: shipping v0.2." in out


# --- recent_wiki_changes --------------------------------------------------

def _set_mtime(path: Path, mtime: float) -> None:
    os.utime(path, (mtime, mtime))


def test_recent_lists_newest_first_with_ages(vault, monkeypatch):
    now = 1_000_000.0
    _set_mtime(vault / "log.md", now - 240)                      # 4m
    _set_mtime(vault / "Projects" / "Iris.md", now - 7_200)      # 2h
    _set_mtime(vault / "Projects" / "PlotProof.md", now - 3 * 86_400)  # 3d
    monkeypatch.setattr(srv, "_now", lambda: now)
    lines = srv.recent_wiki_changes().splitlines()
    assert lines == ["log.md - 4m ago",
                     "Projects/Iris.md - 2h ago",
                     "Projects/PlotProof.md - 3d ago"]


def test_recent_respects_the_limit(vault, monkeypatch):
    now = 1_000_000.0
    _set_mtime(vault / "log.md", now - 60)
    _set_mtime(vault / "Projects" / "Iris.md", now - 7_200)
    _set_mtime(vault / "Projects" / "PlotProof.md", now - 9_000)
    monkeypatch.setattr(srv, "_now", lambda: now)
    assert srv.recent_wiki_changes(limit=1) == "log.md - 1m ago"


def test_recent_on_an_empty_vault_is_a_friendly_string(tmp_path, monkeypatch):
    root = tmp_path / "empty"
    root.mkdir()
    monkeypatch.setattr(srv, "WIKI_DIR", root)
    out = srv.recent_wiki_changes()
    assert "no" in out.lower()


def test_recent_skips_hidden_folders_and_escaping_symlinks(vault, tmp_path,
                                                           monkeypatch):
    hidden = vault / ".git"
    hidden.mkdir()
    (hidden / "COMMIT.md").write_text("internal\n", "utf-8")
    os.symlink(tmp_path / "secret.md", vault / "Leak.md")
    monkeypatch.setattr(srv, "_now", lambda: 1_000_000.0)
    out = srv.recent_wiki_changes()
    assert "COMMIT.md" not in out
    assert "Leak.md" not in out
