"""Tests for the proactive leash (iris/proactive.py)."""

from __future__ import annotations

import io
import json

from iris.proactive import (
    UsageCache,
    fetch_weekly_utilization,
    proactive_allowed,
    read_oauth_token,
)


def test_proactive_allowed_gates_on_headroom_and_park():
    assert proactive_allowed(6.0, parked=False) is True       # tons of headroom
    assert proactive_allowed(79.9, parked=False) is True      # just under
    assert proactive_allowed(80.0, parked=False) is False     # at the line: stop
    assert proactive_allowed(95.0, parked=False) is False     # over
    assert proactive_allowed(6.0, parked=True) is False       # guard parked
    assert proactive_allowed(None, parked=False) is False     # unknown -> fail safe


def test_read_oauth_token(tmp_path):
    p = tmp_path / "creds.json"
    p.write_text(json.dumps({"claudeAiOauth": {"accessToken": "tok-123"}}), "utf-8")
    assert read_oauth_token(p) == "tok-123"
    assert read_oauth_token(tmp_path / "missing.json") is None
    p.write_text("{not json", "utf-8")
    assert read_oauth_token(p) is None


def _opener(payload):
    def opener(req, timeout=None):
        return io.BytesIO(json.dumps(payload).encode())
    return opener


def test_fetch_weekly_utilization_parses_seven_day():
    payload = {"five_hour": {"utilization": 14.0}, "seven_day": {"utilization": 6.0}}
    assert fetch_weekly_utilization("tok", opener=_opener(payload)) == 6.0


def test_fetch_weekly_utilization_handles_failure_and_missing():
    def boom(req, timeout=None):
        raise OSError("network down")
    assert fetch_weekly_utilization("tok", opener=boom) is None
    assert fetch_weekly_utilization("", opener=_opener({})) is None  # no token
    assert fetch_weekly_utilization("tok", opener=_opener({"seven_day": {}})) is None


def test_usage_cache_serves_fresh_without_fetching(tmp_path):
    cache = UsageCache(tmp_path / "u.json")
    calls = []
    cache.get(now=1000.0, fetcher=lambda: calls.append("a") or 6.0)   # primes the cache
    val = cache.get(now=1100.0, fetcher=lambda: calls.append("b") or 9.0)  # 100s later: fresh
    assert val == 6.0
    assert calls == ["a"]  # second call did not refetch


def test_usage_cache_refetches_when_stale(tmp_path):
    cache = UsageCache(tmp_path / "u.json")
    cache.get(now=0.0, fetcher=lambda: 6.0)
    val = cache.get(now=10_000.0, fetcher=lambda: 42.0)  # well past max_age
    assert val == 42.0


def test_usage_cache_keeps_last_value_when_refetch_fails(tmp_path):
    cache = UsageCache(tmp_path / "u.json")
    cache.get(now=0.0, fetcher=lambda: 6.0)
    val = cache.get(now=10_000.0, fetcher=lambda: None)  # stale + fetch fails
    assert val == 6.0  # graceful degrade to last known
