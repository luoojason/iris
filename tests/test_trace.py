"""Tests for the trace ledger (iris/trace.py)."""

from __future__ import annotations

import json

from iris.driver import ClaudeResult
from iris.trace import classify_error, record_trace


def _ok(**kw):
    base = dict(text="hi", session_id="s1", is_error=False, model="m",
                cost_usd=0.2, context_tokens=1000, num_turns=3, duration_ms=1200)
    base.update(kw)
    return ClaudeResult(**base)


def _err(error):
    return ClaudeResult(text="", session_id=None, is_error=True, error=error)


def test_classify_error_returns_none_on_success():
    assert classify_error(_ok()) is None


def test_classify_error_categories():
    assert classify_error(_err("No conversation found for session abc")) == "dead_session"
    assert classify_error(_err("prompt is too long: exceeds the maximum context")) == "context_overflow"
    assert classify_error(_err("claude timed out after 300s")) == "timeout"
    assert classify_error(_err("HTTP 429 rate limit exceeded")) == "rate_limit"
    assert classify_error(_err("usage limit reached for this account")) == "usage_limit"
    assert classify_error(_err("")) == "unknown"
    assert classify_error(_err("something weird happened")) == "other"


def test_record_trace_writes_a_jsonl_record(tmp_path):
    path = tmp_path / "trace.jsonl"
    record_trace(str(path), "chat", _ok(), prompt="hello", session_id="s0")
    lines = path.read_text("utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["kind"] == "chat"
    assert rec["model"] == "m"
    assert rec["is_error"] is False
    assert rec["error_category"] is None
    assert rec["cost_usd"] == 0.2
    assert rec["num_turns"] == 3
    assert rec["context_tokens"] == 1000
    assert "ts" in rec


def test_record_trace_appends(tmp_path):
    path = tmp_path / "trace.jsonl"
    record_trace(str(path), "chat", _ok(), prompt="a")
    record_trace(str(path), "job", _ok(), prompt="b")
    assert len(path.read_text("utf-8").strip().splitlines()) == 2


def test_record_trace_omits_content_by_default(tmp_path):
    path = tmp_path / "trace.jsonl"
    record_trace(str(path), "job", _err("boom: secret detail"), prompt="my secret prompt")
    rec = json.loads(path.read_text("utf-8").strip())
    assert "prompt" not in rec
    assert "result_text" not in rec
    assert "error" not in rec          # the raw message can echo content; omit by default
    assert rec["error_category"] == "other"  # ...but the category is always kept


def test_record_trace_captures_content_when_enabled(tmp_path):
    path = tmp_path / "trace.jsonl"
    record_trace(str(path), "chat", _ok(text="the reply"), prompt="the prompt",
                 capture_content=True)
    rec = json.loads(path.read_text("utf-8").strip())
    assert rec["prompt"] == "the prompt"
    assert rec["result_text"] == "the reply"


def test_record_trace_no_path_is_a_noop(tmp_path):
    record_trace("", "chat", _ok(), prompt="x")  # must not raise


def test_load_and_summarize_traces(tmp_path):
    from iris.trace import load_traces, summarize_traces

    path = tmp_path / "trace.jsonl"
    record_trace(str(path), "chat", _ok(cost_usd=0.1, num_turns=2, duration_ms=1000))
    record_trace(str(path), "job", _ok(cost_usd=0.5, num_turns=4, duration_ms=3000))
    record_trace(str(path), "job", _err("No conversation found"))
    records = load_traces(str(path))
    assert len(records) == 3
    s = summarize_traces(records)
    assert s["runs"] == 3
    assert s["errors"] == 1
    assert round(s["error_rate"], 3) == round(1 / 3, 3)
    assert s["by_kind"] == {"chat": 1, "job": 2}
    assert s["by_error_category"] == {"dead_session": 1}
    assert round(s["total_cost_usd"], 4) == 0.6
    assert s["total_turns"] == 6
    assert s["avg_duration_ms"] == 2000  # only successful turns carry a duration here


def test_load_traces_filters_by_since_and_skips_bad_lines(tmp_path):
    from iris.trace import load_traces

    path = tmp_path / "trace.jsonl"
    path.write_text(
        '{"ts": 100, "kind": "chat"}\n'
        "not json\n"
        '{"ts": 200, "kind": "job"}\n',
        encoding="utf-8",
    )
    assert [r["ts"] for r in load_traces(str(path))] == [100, 200]
    assert [r["ts"] for r in load_traces(str(path), since_ts=150)] == [200]


def test_load_traces_missing_file_is_empty(tmp_path):
    from iris.trace import load_traces

    assert load_traces(str(tmp_path / "absent.jsonl")) == []


def test_render_digest_is_readable_and_model_free(tmp_path):
    from iris.trace import render_digest, summarize_traces

    s = summarize_traces([
        {"kind": "chat", "is_error": False, "cost_usd": 0.1, "num_turns": 2, "duration_ms": 1000},
        {"kind": "job", "is_error": True, "error_category": "timeout"},
    ])
    text = render_digest(s, days=7)
    assert "2 runs" in text
    assert "timeout" in text  # the error taxonomy shows up
    assert "$" in text        # cost is reported


def test_record_trace_is_fail_soft(tmp_path):
    # An unwritable path must never raise (telemetry can't break a turn).
    record_trace(str(tmp_path / "nope" / "deep" / "trace.jsonl"), "chat", _ok(), prompt="x")
    # directory is auto-created, so write a genuinely bad path: a file as a dir
    f = tmp_path / "afile"
    f.write_text("x", "utf-8")
    record_trace(str(f / "trace.jsonl"), "chat", _ok(), prompt="x")  # must not raise
