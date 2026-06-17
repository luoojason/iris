"""Tests for the shared clock-work leash (iris/leash.py)."""

from __future__ import annotations

from iris.config import Config
from iris.driver import ClaudeResult
from iris.leash import clock_work_allowed


def _cfg(tmp_path, **kw):
    base = dict(
        usage_file=str(tmp_path / "usage.json"),
        proactive_usage_cache=str(tmp_path / "cache.json"),
        proactive_usage_max=80.0,
        usage_budget_usd=0.0,  # guard disabled unless a test sets a budget
    )
    base.update(kw)
    return Config(**base)


def test_clock_work_allowed_with_headroom(tmp_path):
    ok, reason = clock_work_allowed(_cfg(tmp_path), now=1000.0, fetch=lambda: 10.0)
    assert ok is True
    assert "util=10.0" in reason and "parked=False" in reason


def test_clock_work_blocked_over_threshold(tmp_path):
    ok, _ = clock_work_allowed(_cfg(tmp_path), now=1000.0, fetch=lambda: 95.0)
    assert ok is False


def test_clock_work_blocked_on_unknown_usage(tmp_path):
    ok, _ = clock_work_allowed(_cfg(tmp_path), now=1000.0, fetch=lambda: None)
    assert ok is False  # unknown utilization fails safe


def test_clock_work_blocked_when_the_guard_is_parked(tmp_path):
    from iris.usage import CreditGuard

    cfg = _cfg(tmp_path, usage_budget_usd=1.0)
    CreditGuard.from_config(cfg).record(
        "job", ClaudeResult(text="", session_id=None, is_error=False, cost_usd=1.0))
    ok, reason = clock_work_allowed(cfg, now=1000.0, fetch=lambda: 1.0)  # tons of headroom...
    assert ok is False  # ...but the guard is parked, so no self-initiated spend
    assert "parked=True" in reason
