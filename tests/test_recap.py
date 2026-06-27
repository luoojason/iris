"""Tests for the instant, zero-inference per-conversation recap (iris/recap.py)."""

from __future__ import annotations

import json
import os

from iris.recap import build_recap, latest_transcript, parse_transcript


def _lines(rows):
    """Rows of (role, content) as JSONL strings, the shape parse_transcript reads."""
    return [json.dumps({"message": {"role": role, "content": content}}) for role, content in rows]


def _write_transcript(path, rows):
    path.write_text("\n".join(_lines(rows)) + "\n", encoding="utf-8")


def _sample_rows():
    return [
        ("user", "please fix the parser bug"),
        ("assistant", "Looking into it now."),
        ("assistant", [
            {"type": "text", "text": "Editing the file."},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/repo/iris/recap.py"}},
        ]),
        # A tool_result comes back as a user-role message with no text; it must
        # not count as a visible user turn.
        ("user", [{"type": "tool_result", "content": "ok"}]),
        ("user", "thanks, ship it"),
        ("assistant", "Shipped. All tests pass."),
    ]


def test_parse_counts_visible_turns_and_skips_tool_results():
    data = parse_transcript(_lines(_sample_rows()))
    assert data["user_turns"] == 2          # the two real asks; tool_result skipped
    assert data["assistant_turns"] == 3     # the three replies that carry text


def test_parse_extracts_tool_names_and_edited_files():
    data = parse_transcript(_lines(_sample_rows()))
    assert "edited files" in data["tools"]                  # Edit folded to a friendly verb
    assert data["tools"]["edited files"] == 1
    assert "/repo/iris/recap.py" in data["edited_files"]    # path captured raw


def test_parse_tracks_last_ask_and_reply():
    data = parse_transcript(_lines(_sample_rows()))
    assert data["last_user"] == "thanks, ship it"
    assert data["last_assistant"] == "Shipped. All tests pass."


def test_parse_is_defensive_about_junk_lines():
    lines = [
        "not json at all",
        "",
        json.dumps({"message": {"role": "user", "content": "real ask"}}),
        json.dumps(["unexpected", "shape"]),                 # not a dict
        json.dumps({"message": {"role": "assistant"}}),       # missing content
    ]
    data = parse_transcript(lines)
    assert data["user_turns"] == 1
    assert data["last_user"] == "real ask"
    assert data["assistant_turns"] == 0


def test_parse_reply_is_truncated():
    data = parse_transcript(_lines([("assistant", "x" * 1000)]))
    assert len(data["last_assistant"]) <= 280


def test_build_recap_renders_local_summary(tmp_path):
    path = tmp_path / "session.jsonl"
    _write_transcript(path, _sample_rows())
    recap = build_recap(str(path))
    assert "2 from you" in recap
    assert "3 from me" in recap
    assert "edited files" in recap
    assert "/repo/iris/recap.py" in recap
    assert "thanks, ship it" in recap
    assert "Shipped. All tests pass." in recap


def test_build_recap_missing_file_is_graceful(tmp_path):
    assert "no transcript" in build_recap(str(tmp_path / "nope.jsonl")).lower()


def test_latest_transcript_picks_most_recent(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    old = proj / "old.jsonl"
    new = proj / "new.jsonl"
    _write_transcript(old, [("user", "old")])
    _write_transcript(new, [("user", "new")])
    os.utime(old, (1, 1))
    os.utime(new, (10_000_000, 10_000_000))
    assert latest_transcript(str(tmp_path)) == str(new)


def test_latest_transcript_empty_dir_is_none(tmp_path):
    assert latest_transcript(str(tmp_path / "empty")) is None
