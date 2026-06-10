"""Usage MCP server tests: one read-only, friendly-string tool over the
metrics JSONL.

Seams mirror test_jobs_server.py: ``METRICS_FILE`` and ``MONTHLY_CREDIT`` are
the module's env-derived config (monkeypatched per test) and ``_now`` is its
one clock. The tool never raises and never spawns anything.
"""

from __future__ import annotations

import json
import socket
import subprocess
from datetime import datetime

import pytest

pytest.importorskip("mcp")  # the server needs the MCP SDK; skip if absent

from iris.mcp import usage_server as srv


def metric(ts, cost, conversation_id="discord:1", model="claude-sonnet-4-6", **over):
    rec = {"ts": ts, "conversation_id": conversation_id, "model": model,
           "cost_usd": cost, "context_tokens": 1000, "is_error": False}
    rec.update(over)
    return rec


def write_metrics(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


@pytest.fixture
def metrics_file(tmp_path, monkeypatch):
    path = tmp_path / "m.jsonl"
    monkeypatch.setattr(srv, "METRICS_FILE", str(path))
    monkeypatch.setattr(srv, "MONTHLY_CREDIT", 0.0)
    monkeypatch.setattr(srv, "_now", lambda: datetime(2026, 6, 16).timestamp())
    return path


def test_usage_summary_renders_the_month_by_default(metrics_file):
    write_metrics(metrics_file, [
        metric(datetime(2026, 6, 5).timestamp(), 0.20, model="claude-opus-4-6"),
        metric(datetime(2026, 6, 10).timestamp(), 0.05, conversation_id="job:3"),
        metric(datetime(2026, 5, 20).timestamp(), 9.99),  # last month: excluded
    ])
    out = srv.usage_summary()
    assert "spend: $0.25 (2 turns)" in out
    assert "claude-opus-4-6: $0.20" in out
    assert "job: $0.05" in out
    assert "used" not in out  # no credit configured: no credit lines


def test_usage_summary_credit_and_projection_lines(metrics_file, monkeypatch):
    monkeypatch.setattr(srv, "MONTHLY_CREDIT", 100.0)
    write_metrics(metrics_file, [metric(datetime(2026, 6, 5).timestamp(), 40.0)])
    out = srv.usage_summary("month")
    assert "credit: $40.00 of $100.00 (40.0% used)" in out
    assert "projected month end: $80.00" in out  # half of June elapsed


def test_usage_summary_day_period_narrows_and_skips_credit_lines(metrics_file, monkeypatch):
    monkeypatch.setattr(srv, "MONTHLY_CREDIT", 100.0)
    monkeypatch.setattr(srv, "_now", lambda: datetime(2026, 6, 16, 12).timestamp())
    write_metrics(metrics_file, [
        metric(datetime(2026, 6, 16, 1).timestamp(), 0.10),
        metric(datetime(2026, 6, 15).timestamp(), 5.00),  # yesterday: excluded
    ])
    out = srv.usage_summary("day")
    assert "spend: $0.10 (1 turns)" in out
    assert "used" not in out  # day spend vs a monthly credit would mislead


def test_usage_summary_unknown_period_is_a_friendly_string(metrics_file):
    out = srv.usage_summary("year")
    assert "year" in out
    for valid in ("day", "week", "month"):
        assert valid in out


def test_usage_summary_without_a_configured_file_is_friendly(monkeypatch):
    monkeypatch.setattr(srv, "METRICS_FILE", "")
    out = srv.usage_summary()
    assert "IRIS_METRICS_FILE" in out


def test_usage_summary_missing_file_renders_a_zero_summary(metrics_file):
    out = srv.usage_summary()  # the file was never written
    assert "spend: $0.00 (0 turns)" in out


def test_usage_summary_tolerates_a_garbage_file(metrics_file):
    metrics_file.write_text("{not json\n\nplain noise\n", encoding="utf-8")
    out = srv.usage_summary()
    assert isinstance(out, str)
    assert "spend: $0.00 (0 turns)" in out


def test_usage_summary_never_touches_network_or_subprocess(metrics_file, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("usage_summary must not open sockets or spawn processes")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(srv, "MONTHLY_CREDIT", 100.0)
    write_metrics(metrics_file, [metric(datetime(2026, 6, 5).timestamp(), 40.0)])
    assert "spend: $40.00" in srv.usage_summary("month")
