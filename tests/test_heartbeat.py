"""Tests for the quiet heartbeat (iris/heartbeat.py): a level-triggered health
checklist that stays silent when all checks pass and pings by exception."""

from __future__ import annotations

from iris.config import Config
from iris.heartbeat import _evaluate, tick_heartbeat, validate_checks
from iris.inbox import Inbox


# -- validation --------------------------------------------------------------

def test_validate_checks_accepts_well_formed_checks():
    checks = [
        {"name": "disk", "kind": "disk_free", "path": "/", "min_percent": 10},
        {"name": "backup", "kind": "file_fresh", "path": "/tmp/b", "max_age_secs": 86400},
        {"name": "site", "kind": "url_ok", "url": "https://example.com"},
    ]
    assert validate_checks(checks) == []


def test_validate_checks_flags_bad_checks():
    problems = validate_checks([
        {"name": "Bad Name", "kind": "disk_free", "path": "/", "min_percent": 10},
        {"name": "x", "kind": "nonsense"},
        {"name": "y", "kind": "url_ok", "url": "ftp://nope"},
        {"name": "z", "kind": "file_fresh", "path": "/tmp/b"},  # no max_age_secs
    ])
    assert len(problems) >= 4


# -- per-check evaluation ----------------------------------------------------

def test_evaluate_disk_free():
    Usage = lambda total, free: type("U", (), {"total": total, "free": free})()
    ok, _ = _evaluate({"kind": "disk_free", "path": "/", "min_percent": 10},
                      now=0.0, disk_usage=lambda p: Usage(100, 50))
    assert ok is True
    bad, detail = _evaluate({"kind": "disk_free", "path": "/", "min_percent": 10},
                            now=0.0, disk_usage=lambda p: Usage(100, 5))
    assert bad is False and "%" in detail


def test_evaluate_mem_free():
    check = {"name": "mem", "kind": "mem_free", "min_percent": 15}
    bad, detail = _evaluate(check, now=0.0, mem_pct=lambda: 10.0)
    assert bad is False and "10" in detail
    ok, _ = _evaluate(check, now=0.0, mem_pct=lambda: 40.0)
    assert ok is True
    # unmeasurable (non-Linux / no /proc) must not raise a false alarm
    ok2, _ = _evaluate(check, now=0.0, mem_pct=lambda: None)
    assert ok2 is True


def test_validate_accepts_mem_free_and_rejects_bad_percent():
    from iris.heartbeat import validate_checks
    assert validate_checks([{"name": "m", "kind": "mem_free", "min_percent": 15}]) == []
    assert validate_checks([{"name": "m", "kind": "mem_free", "min_percent": 0}])  # out of range


def test_system_mem_available_pct_is_a_percent_or_none():
    from iris.heartbeat import system_mem_available_pct
    v = system_mem_available_pct()
    assert v is None or (0.0 <= v <= 100.0)


def test_evaluate_file_fresh(tmp_path):
    f = tmp_path / "beat"
    f.write_text("x", "utf-8")
    import os
    os.utime(f, (1000.0, 1000.0))
    ok, _ = _evaluate({"kind": "file_fresh", "path": str(f), "max_age_secs": 100},
                      now=1050.0)
    assert ok is True
    stale, detail = _evaluate({"kind": "file_fresh", "path": str(f), "max_age_secs": 100},
                              now=2000.0)
    assert stale is False and detail
    missing, detail = _evaluate({"kind": "file_fresh", "path": str(tmp_path / "nope"),
                                 "max_age_secs": 100}, now=2000.0)
    assert missing is False and "missing" in detail.lower()


def test_evaluate_url_ok():
    ok, _ = _evaluate({"kind": "url_ok", "url": "https://e.com"}, now=0.0,
                      fetch=lambda url, timeout: 200)
    assert ok is True
    bad, detail = _evaluate({"kind": "url_ok", "url": "https://e.com", "expect_status": 200},
                            now=0.0, fetch=lambda url, timeout: 503)
    assert bad is False and "503" in detail
    # an unreachable URL is a failure, not a crash
    def boom(url, timeout):
        raise OSError("down")
    unreachable, detail = _evaluate({"kind": "url_ok", "url": "https://e.com"}, now=0.0, fetch=boom)
    assert unreachable is False and detail


# -- tick --------------------------------------------------------------------

def _cfg(tmp_path, **kw):
    base = dict(home_channel="home-1", discord_token="tok",
                heartbeat_file=str(tmp_path / "hb.json"),
                heartbeat_state=str(tmp_path / "hb.state.json"),
                inbox_file=str(tmp_path / "inbox.json"))
    base.update(kw)
    return Config(**base)


def _write(tmp_path, checks):
    import json
    (tmp_path / "hb.json").write_text(json.dumps(checks), "utf-8")


def test_tick_stays_silent_when_all_checks_pass(tmp_path):
    _write(tmp_path, [{"name": "site", "kind": "url_ok", "url": "https://e.com"}])
    sent = []
    line = tick_heartbeat(_cfg(tmp_path), now=1.0,
                          send=lambda c, t, k: sent.append(t) or True,
                          fetch=lambda url, timeout: 200)
    assert sent == []  # silent by default
    assert "0 failing" in line or "ok" in line.lower()


def test_tick_pings_once_with_a_consolidated_digest_on_failure(tmp_path):
    _write(tmp_path, [
        {"name": "site", "kind": "url_ok", "url": "https://e.com"},
        {"name": "api", "kind": "url_ok", "url": "https://api.e.com"},
    ])
    sent = []
    cfg = _cfg(tmp_path)
    # both fail
    tick_heartbeat(cfg, now=1.0, send=lambda c, t, k: sent.append((c, t)) or True,
                   fetch=lambda url, timeout: 500)
    assert len(sent) == 1  # ONE consolidated ping for the whole checklist
    channel, text = sent[0]
    assert channel == "home-1"
    assert "site" in text and "api" in text
    # the failure folded into the inbox too
    assert any("site" in n for n in Inbox(cfg.inbox_file).drain("discord:home-1"))


def test_tick_does_not_repeat_a_steady_failure(tmp_path):
    _write(tmp_path, [{"name": "site", "kind": "url_ok", "url": "https://e.com"}])
    sent = []
    cfg = _cfg(tmp_path)
    send = lambda c, t, k: sent.append(t) or True
    tick_heartbeat(cfg, now=1.0, send=send, fetch=lambda url, timeout: 500)
    tick_heartbeat(cfg, now=2.0, send=send, fetch=lambda url, timeout: 500)  # still failing
    assert len(sent) == 1  # no spam while the failing set is unchanged


def test_tick_keeps_the_failure_in_the_inbox_even_without_a_channel(tmp_path):
    # No home/notify channel: nothing can be pinged, but the failure must not be
    # silently lost - it still folds into the agent's next turn via the inbox.
    _write(tmp_path, [{"name": "site", "kind": "url_ok", "url": "https://e.com"}])
    cfg = _cfg(tmp_path, home_channel="", notify_channel="")
    sent = []
    tick_heartbeat(cfg, now=1.0, send=lambda c, t, k: sent.append(t) or True,
                   fetch=lambda url, timeout: 500)
    assert sent == []  # nowhere to ping
    assert any("site" in n for n in Inbox(cfg.inbox_file).drain())


def test_tick_retries_a_failed_ping_instead_of_going_silent(tmp_path):
    # A failed Discord ping must NOT advance the failing-set baseline, or the
    # alert is lost: the next tick would see an unchanged set and stay quiet, so
    # a real health alert that hit a transient send failure would never reach the
    # owner. The change must be re-pinged until it actually lands.
    _write(tmp_path, [{"name": "site", "kind": "url_ok", "url": "https://e.com"}])
    cfg = _cfg(tmp_path)
    attempts = []

    def flaky_send(c, t, k):
        attempts.append(t)
        return len(attempts) >= 2  # first ping fails, the retry succeeds

    tick_heartbeat(cfg, now=1.0, send=flaky_send, fetch=lambda url, timeout: 500)  # fail -> ping fails
    tick_heartbeat(cfg, now=2.0, send=flaky_send, fetch=lambda url, timeout: 500)  # unchanged, but re-pings
    assert len(attempts) == 2  # the failed ping was retried, not silently dropped
    tick_heartbeat(cfg, now=3.0, send=flaky_send, fetch=lambda url, timeout: 500)  # now steady -> quiet
    assert len(attempts) == 2  # no further pings once the alert has landed


def test_tick_announces_recovery_then_goes_quiet(tmp_path):
    _write(tmp_path, [{"name": "site", "kind": "url_ok", "url": "https://e.com"}])
    sent = []
    cfg = _cfg(tmp_path)
    send = lambda c, t, k: sent.append(t) or True
    status = {"code": 500}
    fetch = lambda url, timeout: status["code"]
    tick_heartbeat(cfg, now=1.0, send=send, fetch=fetch)   # fails -> ping
    status["code"] = 200
    tick_heartbeat(cfg, now=2.0, send=send, fetch=fetch)   # recovers -> ping
    tick_heartbeat(cfg, now=3.0, send=send, fetch=fetch)   # steady ok -> silent
    assert len(sent) == 2
    assert "clear" in sent[1].lower() or "recover" in sent[1].lower()
