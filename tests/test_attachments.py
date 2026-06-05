"""Tests for attachment path/prompt helpers (pure, no network)."""

from __future__ import annotations

from iris.attachments import conversation_dir, describe, safe_filename


def test_safe_filename_strips_traversal_and_spaces():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("a b/c.png") == "c.png"
    assert safe_filename(None) == "file"
    assert safe_filename("") == "file"


def test_conversation_dir_creates_and_sanitizes(tmp_path):
    d = conversation_dir(str(tmp_path), "discord:12345")
    assert d.exists() and d.is_dir()
    assert d.parent == tmp_path
    assert ":" not in d.name


def test_describe_appends_paths():
    out = describe("look at this", ["/a/img.png"])
    assert "look at this" in out
    assert "[attached file: /a/img.png]" in out


def test_describe_with_no_text_still_references_files():
    out = describe("", ["/a/img.png"])
    assert "[attached file: /a/img.png]" in out
    assert out.strip()


def test_describe_with_no_paths_is_just_text():
    assert describe("hi", []) == "hi"
