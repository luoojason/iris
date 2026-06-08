"""Tests for the opt-in per-turn metrics emitter."""
import json

from iris.driver import ClaudeResult
from iris.metrics import emit_turn


def _result():
    return ClaudeResult(
        text="hi", session_id="s1", model="claude-haiku-4-5",
        cost_usd=0.001, context_tokens=1234, duration_ms=900, is_error=False,
    )


def test_no_op_when_path_empty(tmp_path):
    emit_turn("", "cli:local", _result(), "light", "trivial", False, 3)
    assert list(tmp_path.iterdir()) == []


def test_writes_jsonl_record(tmp_path):
    path = tmp_path / "m.jsonl"
    emit_turn(str(path), "discord:123", _result(), "light", "trivial", False, 3)
    line = path.read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["conversation_id"] == "discord:123"
    assert rec["transport"] == "discord"
    assert rec["model"] == "claude-haiku-4-5"
    assert rec["routed"] == "light"
    assert rec["routing_reason"] == "trivial"
    assert rec["cost_usd"] == 0.001
    assert rec["duration_ms"] == 900


def test_appends_not_overwrites(tmp_path):
    path = tmp_path / "m.jsonl"
    emit_turn(str(path), "cli:local", _result(), "strong", "too-long", False, 1)
    emit_turn(str(path), "cli:local", _result(), "light", "trivial", False, 2)
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2


def test_never_raises_on_bad_input(tmp_path):
    emit_turn(str(tmp_path), "cli:local", _result(), "light", "trivial", False, 1)
