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


def test_usage_fetch_opener_refuses_to_follow_redirects():
    # The default opener carries the raw OAuth token in the Authorization header;
    # a 30x to another host must not be auto-followed (it would forward the token).
    import urllib.error

    from iris.proactive import _NoRedirectHandler, _no_redirect_opener

    assert any(isinstance(h, _NoRedirectHandler) for h in _no_redirect_opener.handlers)

    class _Req:
        full_url = "https://api.anthropic.com/api/oauth/usage"

    handler = _NoRedirectHandler()
    try:
        handler.redirect_request(_Req(), None, 302, "Found", {}, "https://evil.example/steal")
        raised = False
    except urllib.error.HTTPError:
        raised = True
    assert raised  # the redirect was refused, not followed


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


def test_usage_cache_survives_a_write_failure(tmp_path):
    # A read-only / full cache dir must not crash the cron tick: get() returns the
    # fresh value even when it can't persist it.
    cache = UsageCache(tmp_path / "cache.json")

    def boom(*a, **k):
        raise OSError("read-only filesystem")

    cache._save = boom
    assert cache.get(1000.0, lambda: 42.0) == 42.0  # fresh value used despite save failure


def test_usage_cache_keeps_last_value_when_refetch_fails(tmp_path):
    cache = UsageCache(tmp_path / "u.json")
    cache.get(now=0.0, fetcher=lambda: 6.0)
    val = cache.get(now=10_000.0, fetcher=lambda: None)  # stale + fetch fails
    assert val == 6.0  # graceful degrade to last known


# -- run_proactive_tick ------------------------------------------------------

from iris.config import Config
from iris.proactive import run_proactive_tick


class _FakeAgent:
    def __init__(self, reply):
        self._reply = reply
        self.calls = []

    def respond(self, conversation_id, text, *a, **kw):
        self.calls.append((conversation_id, text))

        class R:
            text = self._reply
        return R()


def _cfg(tmp_path, **kw):
    base = dict(proactive_enabled=True, home_channel="home-1", discord_token="tok",
                proactive_usage_cache=str(tmp_path / "weekly.json"),
                usage_file=str(tmp_path / "usage.json"))
    base.update(kw)
    return Config(**base)


def test_tick_disabled_does_nothing(tmp_path):
    agent = _FakeAgent("anything")
    cfg = _cfg(tmp_path, proactive_enabled=False)
    assert run_proactive_tick(cfg, "assist", now=1.0, agent=agent, fetch=lambda: 5.0) == "disabled"
    assert agent.calls == []  # never ran a turn


def test_tick_skips_when_over_the_weekly_threshold(tmp_path):
    agent = _FakeAgent("found work")
    sent = []
    status = run_proactive_tick(_cfg(tmp_path), "assist", now=1.0, agent=agent,
                                fetch=lambda: 90.0,
                                sender=lambda c, t, k: sent.append(t))
    assert status.startswith("skipped")
    assert agent.calls == [] and sent == []  # no model call, no spend


def test_tick_silent_reply_posts_nothing(tmp_path):
    agent = _FakeAgent("NOTHING")
    sent = []
    status = run_proactive_tick(_cfg(tmp_path), "assist", now=1.0, agent=agent,
                                fetch=lambda: 6.0,
                                sender=lambda c, t, k: sent.append(t))
    assert status == "silent"
    assert agent.calls and sent == []  # it ran, found nothing, stayed quiet


def test_tick_delivers_a_real_reply_to_the_home_channel(tmp_path):
    agent = _FakeAgent("Cleaned up the wiki index and added a lesson.")
    sent = []
    status = run_proactive_tick(_cfg(tmp_path), "maintain", now=1.0, agent=agent,
                                fetch=lambda: 6.0,
                                sender=lambda c, t, k: sent.append((c, t)))
    assert status == "delivered"
    assert agent.calls[0][0] == "proactive:maintain"   # dedicated session
    assert sent and sent[0][0] == "home-1" and "wiki" in sent[0][1]


def test_proactive_tick_builds_a_clock_gated_agent(tmp_path, monkeypatch):
    # The clock-triggered proactive review must not get the self-starting tools.
    from iris import agent as agent_mod
    from iris.config import Config
    from iris.driver import ClaudeResult

    captured = {}

    class _FakeAgent:
        def respond(self, conv, prompt, *a, **k):
            return ClaudeResult(text="NOTHING", session_id="s", is_error=False)

    def fake_from_config(config, *, clock_gated=False):
        captured["clock_gated"] = clock_gated
        return _FakeAgent()

    monkeypatch.setattr(agent_mod.Agent, "from_config", staticmethod(fake_from_config))
    from iris.proactive import run_proactive_tick
    cfg = Config(proactive_enabled=True, usage_file=str(tmp_path / "u.json"),
                 proactive_usage_cache=str(tmp_path / "c.json"))
    run_proactive_tick(cfg, "assist", now=1000.0, fetch=lambda: 5.0)
    assert captured.get("clock_gated") is True
