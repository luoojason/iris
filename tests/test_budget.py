"""Tests for the pure budget arithmetic over the metrics JSONL."""
import json
import socket
import subprocess
from datetime import datetime

from iris.budget import (
    BudgetState,
    format_summary,
    month_key,
    projection,
    read_metrics,
    summarize,
    thresholds_crossed,
    window,
)


def _rec(**overrides):
    """One synthetic metrics record shaped like metrics.emit_turn output."""
    rec = {
        "ts": 1000.0,
        "conversation_id": "discord:123",
        "transport": "discord",
        "session_id": "s1",
        "model": "claude-haiku-4-5",
        "routed": "light",
        "routing_reason": "trivial",
        "has_attachments": False,
        "cost_usd": 0.01,
        "context_tokens": 1000,
        "duration_ms": 900,
        "is_error": False,
        "turns": 1,
    }
    rec.update(overrides)
    return rec


def _write_jsonl(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


# --- read_metrics ---


def test_read_metrics_missing_file_returns_empty(tmp_path):
    assert read_metrics(tmp_path / "absent.jsonl", 0.0) == []
    assert read_metrics("", 0.0) == []


def test_read_metrics_skips_bad_lines(tmp_path):
    path = tmp_path / "m.jsonl"
    good = _rec(ts=5.0)
    path.write_text(
        json.dumps(good) + "\n"
        + "not json at all\n"
        + "\n"
        + json.dumps([1, 2, 3]) + "\n"
        + json.dumps({"cost_usd": 0.5}) + "\n",  # missing ts: kept at since 0
        encoding="utf-8",
    )
    records = read_metrics(path, 0.0)
    assert len(records) == 2
    assert records[0]["ts"] == 5.0
    assert records[1]["cost_usd"] == 0.5


def test_read_metrics_filters_since_ts_inclusive(tmp_path):
    path = tmp_path / "m.jsonl"
    _write_jsonl(path, [_rec(ts=10.0), _rec(ts=20.0), _rec(ts=30.0)])
    records = read_metrics(path, 20.0)
    assert [r["ts"] for r in records] == [20.0, 30.0]


# --- summarize ---


def test_summarize_totals_and_breakdowns():
    records = [
        _rec(cost_usd=0.20, model="claude-opus-4-6", conversation_id="discord:123"),
        _rec(cost_usd=0.05, model="claude-haiku-4-5", conversation_id="discord:123"),
        _rec(cost_usd=0.30, model="claude-opus-4-6", conversation_id="job:3"),
    ]
    del records[2]["transport"]  # transport must parse from the conversation id
    summary = summarize(records)
    assert summary["turns"] == 3
    assert abs(summary["total_cost"] - 0.55) < 1e-9
    assert abs(summary["by_model"]["claude-opus-4-6"] - 0.50) < 1e-9
    assert abs(summary["by_model"]["claude-haiku-4-5"] - 0.05) < 1e-9
    assert abs(summary["by_transport"]["discord"] - 0.25) < 1e-9
    assert abs(summary["by_transport"]["job"] - 0.30) < 1e-9


def test_summarize_error_rate():
    records = [_rec(), _rec(is_error=True), _rec(), _rec()]
    summary = summarize(records)
    assert summary["errors"] == 1
    assert abs(summary["error_rate"] - 0.25) < 1e-9


def test_summarize_tolerates_missing_fields():
    bare = {"ts": 1.0}  # no cost, model, conversation id, or tokens
    summary = summarize([bare, _rec(cost_usd=0.10)])
    assert summary["turns"] == 2
    assert abs(summary["total_cost"] - 0.10) < 1e-9
    assert summary["by_model"]["unknown"] == 0.0
    assert summary["by_transport"]["unknown"] == 0.0


def test_summarize_top_conversations_capped_at_five():
    records = [
        _rec(conversation_id=f"discord:{i}", cost_usd=float(i)) for i in range(1, 8)
    ]
    top = summarize(records)["top_conversations"]
    assert len(top) == 5
    assert top[0] == ("discord:7", 7.0)
    assert top[-1] == ("discord:3", 3.0)


def test_summarize_context_p95_nearest_rank():
    records = [_rec(context_tokens=i) for i in range(1, 21)]
    assert summarize(records)["context_p95"] == 19
    records = [_rec(context_tokens=i) for i in range(1, 101)]
    assert summarize(records)["context_p95"] == 95
    assert summarize([_rec(context_tokens=42)])["context_p95"] == 42


def test_summarize_empty():
    summary = summarize([])
    assert summary["turns"] == 0
    assert summary["total_cost"] == 0.0
    assert summary["error_rate"] == 0.0
    assert summary["context_p95"] == 0
    assert summary["top_conversations"] == []


# --- window ---


def test_window_day():
    now = datetime(2026, 6, 10, 14, 30, 17).timestamp()
    assert window(now, "day") == datetime(2026, 6, 10).timestamp()


def test_window_week_starts_monday():
    now = datetime(2026, 6, 10, 14, 30).timestamp()  # a Wednesday
    assert window(now, "week") == datetime(2026, 6, 8).timestamp()


def test_window_month():
    now = datetime(2026, 6, 15, 9, 0).timestamp()
    assert window(now, "month") == datetime(2026, 6, 1).timestamp()


def test_window_at_boundaries():
    monday_first = datetime(2026, 6, 1).timestamp()  # Monday, month start
    assert window(monday_first, "day") == monday_first
    assert window(monday_first, "week") == monday_first
    assert window(monday_first, "month") == monday_first


def test_window_rejects_unknown_period():
    try:
        window(0.0, "year")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for unknown period")


# --- projection ---


def test_projection_doubles_at_mid_month():
    now = datetime(2026, 6, 16).timestamp()  # 15 of June's 30 days elapsed
    records = [_rec(cost_usd=10.0), _rec(cost_usd=2.5)]
    assert abs(projection(records, now) - 25.0) < 1e-6


def test_projection_guards_near_month_start():
    now = datetime(2026, 6, 1, 0, 0, 36).timestamp()  # 36s into the month
    estimate = projection([_rec(cost_usd=1.0)], now)
    assert 1.0 < estimate <= 101.0  # clamped, not 1.0 / 1.4e-5


def test_projection_empty_is_zero():
    assert projection([], datetime(2026, 6, 16).timestamp()) == 0.0


# --- thresholds_crossed ---


def test_thresholds_first_crossing():
    assert thresholds_crossed(50.0, 100.0, set()) == [50]
    assert thresholds_crossed(49.99, 100.0, set()) == []


def test_thresholds_multiple_at_once_ascending():
    assert thresholds_crossed(96.0, 100.0, set()) == [50, 80, 95]


def test_thresholds_respects_already_pinged():
    assert thresholds_crossed(96.0, 100.0, {50, 80}) == [95]
    assert thresholds_crossed(96.0, 100.0, {50, 80, 95}) == []


def test_thresholds_zero_credit_disables():
    assert thresholds_crossed(999.0, 0.0, set()) == []
    assert thresholds_crossed(999.0, -5.0, set()) == []


def test_thresholds_exact_boundary():
    assert thresholds_crossed(80.0, 100.0, {50}) == [80]


# --- month_key ---


def test_month_key():
    assert month_key(datetime(2026, 6, 10, 12).timestamp()) == "2026-06"
    assert month_key(datetime(2026, 12, 31, 23, 59).timestamp()) == "2026-12"


# --- BudgetState ---


def test_state_defaults_when_missing(tmp_path):
    state = BudgetState(tmp_path / "b.json")
    assert state.pinged("2026-06") == set()
    assert state.park_until == 0.0


def test_state_roundtrip_persistence(tmp_path):
    path = tmp_path / "b.json"
    state = BudgetState(path)
    state.record_pings("2026-06", [50, 80])
    state.set_park_until(1234.5)
    reloaded = BudgetState(path)
    assert reloaded.pinged("2026-06") == {50, 80}
    assert reloaded.park_until == 1234.5


def test_state_month_rollover_resets_pinged(tmp_path):
    path = tmp_path / "b.json"
    state = BudgetState(path)
    state.record_pings("2026-06", [50, 80, 95])
    # July reads see a clean slate, so thresholds re-arm
    assert state.pinged("2026-07") == set()
    assert thresholds_crossed(60.0, 100.0, state.pinged("2026-07")) == [50]
    state.record_pings("2026-07", [50])
    assert state.pinged("2026-07") == {50}
    assert BudgetState(path).pinged("2026-06") == set()


def test_state_corrupt_file_starts_fresh(tmp_path):
    for garbage in ("{not json", json.dumps([1, 2]), json.dumps({"pinged": "x"})):
        path = tmp_path / "b.json"
        path.write_text(garbage, encoding="utf-8")
        state = BudgetState(path)
        assert state.pinged("2026-06") == set()
        assert state.park_until == 0.0
        state.record_pings("2026-06", [50])
        assert BudgetState(path).pinged("2026-06") == {50}


def test_state_leaves_no_temp_files(tmp_path):
    path = tmp_path / "b.json"
    state = BudgetState(path)
    state.record_pings("2026-06", [50])
    state.set_park_until(99.0)
    assert [f.name for f in tmp_path.iterdir()] == ["b.json"]


# --- format_summary ---


def _sample_summary():
    return summarize([
        _rec(cost_usd=0.20, model="claude-opus-4-6", conversation_id="discord:123",
             context_tokens=1000),
        _rec(cost_usd=0.05, model="claude-haiku-4-5", conversation_id="job:3",
             context_tokens=2000),
        _rec(cost_usd=0.05, model="claude-haiku-4-5", conversation_id="cli:local",
             context_tokens=3000, is_error=True),
    ])


def test_format_summary_contains_breakdowns():
    out = format_summary(_sample_summary())
    assert "spend: $0.30" in out
    assert "claude-opus-4-6: $0.20" in out
    assert "claude-haiku-4-5: $0.10" in out
    assert "job: $0.05" in out
    assert "discord: $0.20" in out
    assert "errors: 1/3" in out
    assert "3000" in out  # context p95
    assert "discord:123: $0.20" in out


def test_format_summary_no_credit_lines_by_default():
    out = format_summary(_sample_summary(), projection=42.0)
    assert "used" not in out
    assert "projected" not in out
    assert "$42.00" not in out


def test_format_summary_credit_and_projection():
    out = format_summary(_sample_summary(), credit=100.0, projection=42.0)
    assert "$100.00" in out
    assert "% used" in out
    assert "$42.00" in out


# --- zero model calls, zero network ---


def test_budget_never_touches_network_or_subprocess(tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("budget must not open sockets or spawn processes")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    path = tmp_path / "m.jsonl"
    _write_jsonl(path, [_rec(ts=datetime(2026, 6, 5).timestamp())])
    now = datetime(2026, 6, 16).timestamp()
    records = read_metrics(path, window(now, "month"))
    summary = summarize(records)
    format_summary(summary, credit=100.0, projection=projection(records, now))
    state = BudgetState(tmp_path / "b.json")
    state.record_pings(month_key(now), thresholds_crossed(0.01, 100.0, set()))
    state.set_park_until(now + 3600)
