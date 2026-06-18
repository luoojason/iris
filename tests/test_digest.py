"""Tests for the session digest (iris/digest.py)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fakes import FakeDriver

from iris.config import Config
from iris.digest import build_digest, gather_day_transcript
from iris.driver import ClaudeResult


def _iso(epoch: float) -> str:
    # ISO with a trailing Z, the format claude transcripts use (and the parse bug magnet).
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _write_transcript(path, rows):
    lines = []
    for role, content, ts in rows:
        lines.append(json.dumps({"message": {"role": role, "content": content}, "timestamp": ts}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


NOW = 1_700_000_000.0


def test_gather_includes_today_excludes_older(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    _write_transcript(proj / "s.jsonl", [
        ("user", "what about the videos today", _iso(NOW - 100)),         # today
        ("assistant", "here is the plan", _iso(NOW - 50)),                # today
        ("user", "ancient history message", _iso(NOW - 5 * 86400)),       # old
    ])
    out = gather_day_transcript(str(tmp_path), since_ts=NOW - 86400)
    assert "videos today" in out
    assert "here is the plan" in out
    assert "ancient history" not in out


def test_gather_handles_list_content_and_skips_empty(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    _write_transcript(proj / "s.jsonl", [
        ("assistant", [{"type": "text", "text": "block text"}], _iso(NOW - 10)),
        ("user", "", _iso(NOW - 10)),  # empty -> skipped
    ])
    out = gather_day_transcript(str(tmp_path), since_ts=NOW - 86400)
    assert "block text" in out


def test_gather_caps_length(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    rows = [("user", "x" * 500, _iso(NOW - 10)) for _ in range(50)]
    _write_transcript(proj / "s.jsonl", rows)
    out = gather_day_transcript(str(tmp_path), since_ts=NOW - 86400, max_chars=2000)
    assert len(out) <= 2200  # capped (+ small header/marker slack)


def test_gather_missing_dir_is_empty(tmp_path):
    assert gather_day_transcript(str(tmp_path / "nope"), since_ts=NOW - 86400) == ""


def test_build_digest_summarizes_a_populated_day(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    _write_transcript(proj / "s.jsonl", [("user", "talked about videos", _iso(NOW - 100))])
    driver = FakeDriver([ClaudeResult(text="Recap: videos.", session_id="d", is_error=False)])
    out = build_digest(Config(), now=NOW, driver=driver, transcripts_dir=str(tmp_path))
    assert out == "Recap: videos."
    assert len(driver.calls) == 1
    prompt = driver.calls[0][0]
    assert "talked about videos" in prompt          # the transcript was fed in
    assert "not instructions" in prompt.lower()     # fenced as data


def test_build_digest_empty_day_makes_no_model_call(tmp_path):
    driver = FakeDriver([])  # would raise if called
    out = build_digest(Config(), now=NOW, driver=driver, transcripts_dir=str(tmp_path))
    assert out == ""
    assert driver.calls == []


def test_build_digest_error_returns_empty(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    _write_transcript(proj / "s.jsonl", [("user", "stuff", _iso(NOW - 100))])
    driver = FakeDriver([ClaudeResult(text="", session_id=None, is_error=True, error="boom")])
    assert build_digest(Config(), now=NOW, driver=driver, transcripts_dir=str(tmp_path)) == ""
