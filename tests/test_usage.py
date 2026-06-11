"""Tests for the credit guard (iris/usage.py)."""

from __future__ import annotations

import json
import os

from iris.config import Config
from iris.driver import ClaudeResult
from iris.usage import (
    CreditGuard,
    UsageLedger,
    budget_tick,
    level_for,
    month_key,
    percent_used,
    record_turn,
    summary_text,
)


def result(cost=0.5, tokens=1000, is_error=False):
    return ClaudeResult(text="x", session_id="s", is_error=is_error,
                        cost_usd=cost, context_tokens=tokens)


NOW = 1780000000.0  # 2026-05-29 UTC, a fixed month


def freeze_usage_clock(monkeypatch):
    """Pin iris.usage's clock so record() and the read-back land in one month
    (the hook tests record and read with now=None)."""
    import iris.usage as usage_mod
    monkeypatch.setattr(usage_mod.time, "time", lambda: NOW)


# -- ledger --------------------------------------------------------------------


def test_month_key_is_utc_year_month():
    assert month_key(0) == "1970-01"
    assert month_key(NOW) == "2026-05"


def test_record_accumulates_by_month_and_source(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.json")
    ledger.record("chat", result(cost=0.5, tokens=100), now=NOW)
    ledger.record("chat", result(cost=0.25, tokens=50), now=NOW)
    ledger.record("job", result(cost=1.0, tokens=200), now=NOW)
    entry = ledger.month(now=NOW)
    assert entry["cost_usd"] == 1.75
    assert entry["turns"] == 3
    assert entry["tokens"] == 350
    assert entry["by_source"] == {"chat": 0.75, "job": 1.0}


def test_record_tolerates_missing_cost_fields(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.json")
    ledger.record("chat", ClaudeResult(text="", session_id=None, is_error=True), now=NOW)
    entry = ledger.month(now=NOW)
    assert entry["turns"] == 1
    assert entry["cost_usd"] == 0.0


def test_months_are_separated(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.json")
    ledger.record("chat", result(cost=1.0), now=NOW)
    ledger.record("chat", result(cost=2.0), now=NOW + 35 * 86400)
    assert ledger.month(now=NOW)["cost_usd"] == 1.0
    assert ledger.month(now=NOW + 35 * 86400)["cost_usd"] == 2.0


def test_record_turn_is_fail_soft(tmp_path):
    # an unwritable path (its parent is a file) must never break a turn
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    record_turn(str(blocker / "x.json"), "chat", result())


def test_corrupt_ledger_starts_fresh(tmp_path):
    path = tmp_path / "usage.json"
    path.write_text("{broken", encoding="utf-8")
    ledger = UsageLedger(path)
    ledger.record("chat", result(cost=1.0), now=NOW)
    assert ledger.month(now=NOW)["cost_usd"] == 1.0


# -- levels ----------------------------------------------------------------------


def test_percent_and_levels():
    assert percent_used({"cost_usd": 40.0}, 50.0) == 80.0
    assert percent_used({"cost_usd": 0.0}, 50.0) == 0.0
    assert percent_used({"cost_usd": 10.0}, 0.0) == 0.0  # no budget = guard off
    assert level_for(0.0, 80.0, 95.0) == "ok"
    assert level_for(80.0, 80.0, 95.0) == "tighten"
    assert level_for(95.0, 80.0, 95.0) == "park"
    assert level_for(150.0, 80.0, 95.0) == "park"


def guard_config(tmp_path, budget=10.0, **kw):
    return Config(
        usage_file=str(tmp_path / "usage.json"),
        usage_budget_usd=budget,
        **kw,
    )


def test_guard_levels_track_the_ledger(tmp_path):
    config = guard_config(tmp_path)
    guard = CreditGuard.from_config(config)
    assert guard.level() == "ok"
    guard.record("chat", result(cost=8.5))
    assert guard.level() == "tighten"
    guard.record("chat", result(cost=1.2))
    assert guard.level() == "park"
    assert guard.should_park() is True


def test_guard_without_budget_never_brakes(tmp_path):
    guard = CreditGuard.from_config(guard_config(tmp_path, budget=0.0))
    guard.record("chat", result(cost=999.0))
    assert guard.level() == "ok"
    assert guard.should_park() is False
    assert guard.tightened_max_chars(140) == 140


def test_guard_tightens_the_trivial_cap(tmp_path):
    config = guard_config(tmp_path)
    guard = CreditGuard.from_config(config)
    assert guard.tightened_max_chars(140) == 140
    guard.record("chat", result(cost=9.0))  # 90% -> tighten
    assert guard.tightened_max_chars(140) == 420  # x3 default


# -- the tick ---------------------------------------------------------------------


def tick_env(tmp_path, budget=10.0):
    config = Config(
        usage_file=str(tmp_path / "usage.json"),
        usage_budget_usd=budget,
        discord_token="tok",
        home_channel="home-7",
    )
    pings = []

    def send(channel, text, token):
        pings.append((channel, text))
        return True

    return config, pings, send


def test_budget_tick_off_without_budget(tmp_path):
    config, pings, send = tick_env(tmp_path, budget=0.0)
    line = budget_tick(config, now=NOW, send=send)
    assert "off" in line
    assert pings == []


def test_budget_tick_pings_each_crossed_threshold_once(tmp_path):
    config, pings, send = tick_env(tmp_path)
    UsageLedger(config.usage_file).record("chat", result(cost=8.6), now=NOW)  # 86%
    line = budget_tick(config, now=NOW, send=send)
    assert "86%" in line
    assert len(pings) == 2  # crossed 50 and 80 together
    assert all(channel == "home-7" for channel, _ in pings)
    assert any("50%" in text for _, text in pings)
    assert any("80%" in text for _, text in pings)
    # the next tick is silent: both crossings were recorded
    line2 = budget_tick(config, now=NOW, send=send)
    assert "pinged" not in line2
    assert len(pings) == 2


def test_budget_tick_failed_send_retries_next_tick(tmp_path):
    config, pings, send = tick_env(tmp_path)
    UsageLedger(config.usage_file).record("chat", result(cost=6.0), now=NOW)  # 60%
    budget_tick(config, now=NOW, send=lambda *a: False)  # delivery down
    budget_tick(config, now=NOW, send=send)
    assert len(pings) == 1  # retried because the failed ping was not recorded


def test_budget_tick_below_every_threshold_is_silent(tmp_path):
    config, pings, send = tick_env(tmp_path)
    UsageLedger(config.usage_file).record("chat", result(cost=1.0), now=NOW)  # 10%
    budget_tick(config, now=NOW, send=send)
    assert pings == []


# -- surfaces ----------------------------------------------------------------------


def test_summary_text_reports_the_month(tmp_path):
    config = guard_config(tmp_path)
    UsageLedger(config.usage_file).record("job", result(cost=2.5), now=NOW)
    text = summary_text(config, now=NOW)
    assert "2026-05" in text
    assert "2.50" in text and "10.00" in text
    assert "25%" in text
    assert "job" in text


def test_cli_usage_command(tmp_path, monkeypatch, capsys):
    from iris.cli import main

    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IRIS_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setenv("IRIS_USAGE_BUDGET_USD", "20")
    assert main(["usage"]) == 0
    out = capsys.readouterr().out
    assert "budget" in out.lower()


def test_usage_mcp_tool(tmp_path, monkeypatch):
    import iris.mcp.usage as srv

    freeze_usage_clock(monkeypatch)
    config = guard_config(tmp_path)
    monkeypatch.setattr(srv, "_CONFIG", config)
    UsageLedger(config.usage_file).record("chat", result(cost=5.0))  # current month
    out = srv.usage_report()
    assert "50%" in out


# -- recording hooks -----------------------------------------------------------------


def test_agent_records_chat_turns(tmp_path, monkeypatch):
    from iris.agent import Agent
    from iris.sessions import SessionStore

    class FakeDriver:
        model = None

        def run(self, prompt, session_id=None, model=None):
            return result(cost=0.3)

    freeze_usage_clock(monkeypatch)
    config = guard_config(tmp_path)
    guard = CreditGuard.from_config(config)
    agent = Agent(FakeDriver(), SessionStore(tmp_path / "s.json"), guard=guard)
    agent.respond("c1", "hello")
    assert UsageLedger(config.usage_file).month()["by_source"] == {"chat": 0.3}


def test_agent_tightened_routing_uses_the_light_model(tmp_path):
    from iris.agent import Agent
    from iris.sessions import SessionStore

    calls = []

    class FakeDriver:
        model = "strong"

        def run(self, prompt, session_id=None, model=None):
            calls.append(model)
            return result(cost=0.0)

    config = guard_config(tmp_path)
    guard = CreditGuard.from_config(config)
    UsageLedger(config.usage_file).record("chat", result(cost=9.0))  # tighten
    agent = Agent(FakeDriver(), SessionStore(tmp_path / "s.json"),
                  light_model="light", guard=guard)
    # 200 plain chars: over the 140 default cap, under the tightened 420 cap
    agent.respond("c1", "ok " * 66)
    assert calls == ["light"]


def test_run_job_records_job_turns(tmp_path, monkeypatch):
    from iris.jobs import JobStore, run_job

    freeze_usage_clock(monkeypatch)
    store = JobStore(tmp_path / "jobs.json")
    job = store.add("t", "do it", ["subagents"], "", "chan")
    config = guard_config(tmp_path, jobs_enabled=True, discord_token="tok")
    guard = CreditGuard.from_config(config)

    class FakeDriver:
        def run(self, prompt, session_id=None, model=None):
            return result(cost=0.7)

    from iris.inbox import Inbox
    from iris.workspaces import WorkspaceStore

    run_job(
        job["id"], config,
        store=store, workspace_store=WorkspaceStore(tmp_path / "ws.json"),
        inbox=Inbox(tmp_path / "inbox.json"),
        driver_factory=lambda c, j, w, cb=None: FakeDriver(),
        send_message=lambda *a: True, send_file=lambda *a: {"ok": True},
        guard=guard,
    )
    assert UsageLedger(config.usage_file).month()["by_source"] == {"job": 0.7}


def test_start_job_parks_at_park_level(tmp_path, monkeypatch):
    import iris.mcp.jobs as srv
    from iris.jobs import JobStore

    config = Config(
        jobs_enabled=True,
        jobs_file=str(tmp_path / "jobs.json"),
        workspaces_file=str(tmp_path / "ws.json"),
        usage_file=str(tmp_path / "usage.json"),
        usage_budget_usd=10.0,
    )
    UsageLedger(config.usage_file).record("chat", result(cost=9.9))  # 99% -> park
    spawned = []
    monkeypatch.setattr(srv, "_CONFIG", config)
    monkeypatch.setattr(srv, "SPAWN", lambda job_id, **kw: spawned.append(job_id))
    reply = srv.start_job("big", "work")
    assert "parked" in reply.lower()
    assert "resume_job(1)" in reply
    assert spawned == []
    assert JobStore(config.jobs_file).get(1)["state"] == "parked"


def test_watch_records_notify_turns(tmp_path, monkeypatch):
    import iris.notify.watch_cmd as wc

    freeze_usage_clock(monkeypatch)
    config = Config(
        discord_token="tok", notify_channel="chan",
        usage_file=str(tmp_path / "usage.json"),
    )

    class FakeDriver:
        def run(self, prompt, session_id=None, model=None):
            return result(cost=0.1)

    sent = []
    rc = wc.watch(
        ["fail.sh"], config, name="fail",
        runner=lambda argv: (1, 5.0, "boom"),
        driver_factory=lambda: FakeDriver(),
        sender=lambda channel, text, token: sent.append(text) or True,
    )
    assert rc == 1
    assert UsageLedger(config.usage_file).month()["by_source"] == {"notify": 0.1}


def test_reminders_tick_prints_the_budget_line(tmp_path, monkeypatch, capsys):
    import iris.reminders as reminders_mod
    from iris.cli import reminders_tick

    monkeypatch.setenv("IRIS_REMINDERS_FILE", str(tmp_path / "rem.json"))
    monkeypatch.setattr(reminders_mod, "send_discord_message", lambda *a: True)
    config = Config(
        discord_token="tok", home_channel="home",
        usage_file=str(tmp_path / "usage.json"), usage_budget_usd=10.0,
    )
    assert reminders_tick(config) == 0
    out = capsys.readouterr().out
    assert "reminders-tick" in out
    assert "budget" in out


# -- config knobs --------------------------------------------------------------------


def test_usage_config_knobs(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IRIS_USAGE_FILE", "u.json")
    monkeypatch.setenv("IRIS_USAGE_BUDGET_USD", "33.5")
    monkeypatch.setenv("IRIS_USAGE_TIGHTEN_AT", "70")
    monkeypatch.setenv("IRIS_USAGE_PARK_AT", "90")
    monkeypatch.setenv("IRIS_USAGE_PING_AT", "25, 75")
    monkeypatch.setenv("IRIS_TIGHTEN_FACTOR", "2")
    cfg = Config.from_env(dotenv=tmp_path / "none.env")
    assert cfg.usage_file == "u.json"
    assert cfg.usage_budget_usd == 33.5
    assert cfg.usage_tighten_at == 70.0
    assert cfg.usage_park_at == 90.0
    assert cfg.usage_ping_at == [25.0, 75.0]
    assert cfg.tighten_factor == 2.0


def test_usage_config_defaults(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    cfg = Config.from_env(dotenv=tmp_path / "none.env")
    assert cfg.usage_file == "iris-usage.json"
    assert cfg.usage_budget_usd == 0.0
    assert cfg.usage_tighten_at == 80.0
    assert cfg.usage_park_at == 95.0
    assert cfg.usage_ping_at == [50.0, 80.0, 95.0]
    assert cfg.tighten_factor == 3.0


def test_ledger_file_is_json_on_disk(tmp_path):
    path = tmp_path / "usage.json"
    UsageLedger(path).record("chat", result(cost=1.0), now=NOW)
    data = json.loads(path.read_text("utf-8"))
    assert "2026-05" in data


def test_budget_tick_pings_at_an_exact_threshold_boundary(tmp_path):
    config, pings, send = tick_env(tmp_path)
    UsageLedger(config.usage_file).record("chat", result(cost=8.0), now=NOW)  # exactly 80%
    budget_tick(config, now=NOW, send=send)
    assert any("80%" in text for _, text in pings)
    assert any("50%" in text for _, text in pings)


def test_guard_parks_at_exactly_park_at(tmp_path):
    config = guard_config(tmp_path)
    guard = CreditGuard.from_config(config)
    UsageLedger(config.usage_file).record("chat", result(cost=9.5))  # exactly 95%
    assert guard.should_park() is True
