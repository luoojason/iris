"""Credit-guard wiring: the driver pushback predicate, the reminders-tick
budget check, and JobRunner.from_config's budget field mapping.

Everything here is file arithmetic and templated strings: fake senders capture
pings, timestamps are injected, and the template-only pin makes any model call
or network touch explode the test. No conftest; fakes are local.
"""

from __future__ import annotations

import json
import socket
import subprocess
from datetime import datetime

from iris.config import Config
from iris.driver import ClaudeDriver, is_credit_or_rate_pushback


# -- is_credit_or_rate_pushback ------------------------------------------------


def test_predicate_matches_credit_exhaustion_markers():
    assert is_credit_or_rate_pushback("Credit balance is too low")
    assert is_credit_or_rate_pushback("insufficient credits remaining")
    assert is_credit_or_rate_pushback("rate_limit")


def test_predicate_matches_rate_limit_markers():
    assert is_credit_or_rate_pushback("rate_limit_error: slow down")
    assert is_credit_or_rate_pushback("HTTP 429 from the API")
    assert is_credit_or_rate_pushback("Overloaded, try again")
    assert is_credit_or_rate_pushback("upstream 529")


def test_predicate_ignores_per_request_defects_and_noise():
    # Auth and bad-request failures are one job's problem; parking the whole
    # fleet on them would be wrong.
    assert not is_credit_or_rate_pushback("authentication_error: bad login")
    assert not is_credit_or_rate_pushback("invalid_request_error: no such model")
    assert not is_credit_or_rate_pushback("not_found_error")
    assert not is_credit_or_rate_pushback("permission_error")
    assert not is_credit_or_rate_pushback("the worker crashed")
    assert not is_credit_or_rate_pushback("")
    assert not is_credit_or_rate_pushback(None)


def test_predicate_ignores_free_form_job_error_text_with_loose_words():
    # Job error text is free-form (model prose, folded stderr); a bare
    # 'insufficient' or 'quota' in it is one job's problem, not the credit
    # pool, and must not park the whole fleet.
    assert not is_credit_or_rate_pushback("insufficient permissions to write the file")
    assert not is_credit_or_rate_pushback("disk quota exceeded")


def test_retry_classifiers_keep_the_loose_markers():
    # Only the PARKING predicate narrowed: the retry classifiers' tuples keep
    # 'insufficient'/'quota' (terminal: surfaced immediately, never retried),
    # and every rate-limit marker still parks.
    from iris.driver import _RATE_LIMIT_MARKERS, _TERMINAL_MARKERS

    for marker in ("credit balance", "insufficient", "quota"):
        assert marker in _TERMINAL_MARKERS
    for marker in _RATE_LIMIT_MARKERS:
        assert is_credit_or_rate_pushback(marker)


# -- the reminders-tick budget check --------------------------------------------


def metric(ts, cost, conversation_id="discord:1", **over):
    rec = {"ts": ts, "conversation_id": conversation_id,
           "model": "claude-sonnet-4-6", "cost_usd": cost,
           "context_tokens": 1000, "is_error": False}
    rec.update(over)
    return rec


def write_metrics(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def collect_sender(sent):
    def sender(channel, text, token):
        sent.append((channel, text, token))
        return True
    return sender


def tick_config(tmp_path, **over):
    fields = dict(
        metrics_file=str(tmp_path / "m.jsonl"),
        budget_state=str(tmp_path / "budget.json"),
        monthly_credit=100.0,
        notify_channel="999",
        discord_token="tok",
    )
    fields.update(over)
    return Config(**fields)


def test_tick_pings_each_newly_crossed_threshold_once(tmp_path):
    from iris.budget import BudgetState
    from iris.cli import budget_tick

    config = tick_config(tmp_path)
    now = datetime(2026, 6, 16).timestamp()  # half of June elapsed
    write_metrics(tmp_path / "m.jsonl", [metric(datetime(2026, 6, 5).timestamp(), 83.12)])
    sent = []

    budget_tick(config, now=now, sender=collect_sender(sent))

    assert [s[0] for s in sent] == ["999", "999"]
    assert sent[0][1] == ("budget: 50% of the monthly agent credit used "
                          "($83.12 of $100.00; projecting $166.24 by month end)")
    assert sent[1][1] == ("budget: 80% of the monthly agent credit used "
                          "($83.12 of $100.00; projecting $166.24 by month end)")
    assert BudgetState(config.budget_state).pinged("2026-06") == {50, 80}

    budget_tick(config, now=now, sender=collect_sender(sent))
    assert len(sent) == 2  # already pinged: silent

    write_metrics(tmp_path / "m.jsonl", [
        metric(datetime(2026, 6, 5).timestamp(), 83.12),
        metric(datetime(2026, 6, 10).timestamp(), 13.0),
    ])
    budget_tick(config, now=now, sender=collect_sender(sent))
    assert len(sent) == 3
    assert sent[2][1] == ("budget: 95% of the monthly agent credit used "
                          "($96.12 of $100.00; projecting $192.24 by month end)")


def test_tick_guard_off_without_credit_or_metrics(tmp_path):
    from iris.cli import budget_tick

    now = datetime(2026, 6, 16).timestamp()
    write_metrics(tmp_path / "m.jsonl", [metric(datetime(2026, 6, 5).timestamp(), 99.0)])
    sent = []

    budget_tick(tick_config(tmp_path, monthly_credit=0.0),
                now=now, sender=collect_sender(sent))
    budget_tick(tick_config(tmp_path, metrics_file=""),
                now=now, sender=collect_sender(sent))

    assert sent == []


def test_tick_rearms_on_month_rollover(tmp_path):
    from iris.budget import BudgetState
    from iris.cli import budget_tick

    config = tick_config(tmp_path)
    BudgetState(config.budget_state).record_pings("2026-06", [50, 80, 95])
    write_metrics(tmp_path / "m.jsonl", [metric(datetime(2026, 7, 5).timestamp(), 60.0)])
    sent = []

    budget_tick(config, now=datetime(2026, 7, 16).timestamp(),
                sender=collect_sender(sent))

    assert len(sent) == 1
    assert sent[0][1].startswith("budget: 50% of the monthly agent credit used")
    assert BudgetState(config.budget_state).pinged("2026-07") == {50}


def test_tick_does_not_record_a_failed_send(tmp_path):
    from iris.budget import BudgetState
    from iris.cli import budget_tick

    config = tick_config(tmp_path)
    now = datetime(2026, 6, 16).timestamp()
    write_metrics(tmp_path / "m.jsonl", [metric(datetime(2026, 6, 5).timestamp(), 55.0)])

    budget_tick(config, now=now, sender=lambda channel, text, token: False)
    assert BudgetState(config.budget_state).pinged("2026-06") == set()

    sent = []
    budget_tick(config, now=now, sender=collect_sender(sent))  # retried next tick
    assert len(sent) == 1
    assert BudgetState(config.budget_state).pinged("2026-06") == {50}


def test_tick_clears_an_expired_park_and_leaves_a_live_one(tmp_path):
    from iris.budget import BudgetState
    from iris.cli import budget_tick

    config = tick_config(tmp_path, monthly_credit=0.0)  # park handling needs no credit
    now = datetime(2026, 6, 16).timestamp()

    BudgetState(config.budget_state).set_park_until(now - 10)
    budget_tick(config, now=now, sender=collect_sender([]))
    assert BudgetState(config.budget_state).park_until == 0.0

    BudgetState(config.budget_state).set_park_until(now + 100)
    budget_tick(config, now=now, sender=collect_sender([]))
    assert BudgetState(config.budget_state).park_until == now + 100


def test_tick_pings_resumed_when_it_clears_an_expired_park(tmp_path):
    # In the standard deployment the tick usually beats the runner to an
    # expired park; whoever clears it owes the documented "jobs resumed" ping.
    from iris.budget import BudgetState
    from iris.cli import budget_tick

    config = tick_config(tmp_path, monthly_credit=0.0)
    now = datetime(2026, 6, 16).timestamp()
    BudgetState(config.budget_state).set_park_until(now - 10)
    sent = []

    budget_tick(config, now=now, sender=collect_sender(sent))

    assert BudgetState(config.budget_state).park_until == 0.0
    assert [s[:2] for s in sent] == [("999", "jobs resumed: the budget park expired")]

    budget_tick(config, now=now, sender=collect_sender(sent))  # already cleared: silent
    assert len(sent) == 1


def test_whoever_clears_the_park_pings_and_the_other_side_stays_silent(tmp_path):
    # The state transition is the dedupe: once the tick cleared (and pinged),
    # the runner sees no park and must not ping a second time.
    from iris.budget import BudgetState
    from iris.cli import budget_tick
    from iris.jobs import JobRunner, JobStore

    config = tick_config(tmp_path, monthly_credit=0.0,
                         jobs_file=str(tmp_path / "jobs.json"))
    now = datetime(2026, 6, 16).timestamp()
    BudgetState(config.budget_state).set_park_until(now - 10)
    sent = []
    budget_tick(config, now=now, sender=collect_sender(sent))
    resumes = [t for _, t, _ in sent if t.startswith("jobs resumed")]
    assert resumes == ["jobs resumed: the budget park expired"]

    runner = JobRunner(JobStore(config.jobs_file), ClaudeDriver(), sync=True,
                       budget_state_path=config.budget_state,
                       notify_channel="999", discord_token="tok",
                       sender=collect_sender(sent))
    runner.check_now()

    assert len([t for _, t, _ in sent if t.startswith("jobs resumed")]) == 1


def test_reminders_tick_budget_path_is_template_only(tmp_path, monkeypatch):
    # The tick runs on a clock, so by rule it may never spend a model call:
    # the driver class itself, sockets, and subprocess all explode here.
    import iris.driver as driver_mod
    from iris.cli import reminders_tick

    def explode(*args, **kwargs):
        raise AssertionError("the tick must never build a driver or touch the network")

    monkeypatch.setattr(driver_mod, "ClaudeDriver", explode)
    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setenv("IRIS_REMINDERS_FILE", str(tmp_path / "reminders.json"))
    config = tick_config(tmp_path)
    write_metrics(tmp_path / "m.jsonl", [metric(datetime(2026, 6, 5).timestamp(), 83.12)])
    sent = []

    rc = reminders_tick(config, now=datetime(2026, 6, 16).timestamp(),
                        sender=collect_sender(sent))

    assert rc == 0
    assert len(sent) == 2  # 50 and 80 crossed, one templated ping each


# -- JobRunner.from_config: budget fields ----------------------------------------


def test_from_config_maps_the_budget_fields(tmp_path):
    from iris.jobs import JobRunner

    config = Config(
        jobs_file=str(tmp_path / "jobs.json"),
        monthly_credit=100.0,
        budget_state=str(tmp_path / "budget.json"),
        budget_park_minutes=30.0,
        light_model="claude-haiku-4-5",
    )

    runner = JobRunner.from_config(config, ClaudeDriver())

    assert runner.budget_state_path == str(tmp_path / "budget.json")
    assert runner.monthly_credit == 100.0
    assert runner.light_model == "claude-haiku-4-5"
    assert runner.park_minutes == 30.0


def test_a_budget_tick_failure_never_sinks_the_reminders(tmp_path, monkeypatch):
    # The reminders delivery is the tick's first duty; a budget state file that
    # cannot be written must not take it down.
    from iris.cli import reminders_tick

    monkeypatch.setenv("IRIS_REMINDERS_FILE", str(tmp_path / "reminders.json"))
    config = tick_config(tmp_path, budget_state=str(tmp_path))  # a directory: unwritable
    write_metrics(tmp_path / "m.jsonl", [metric(datetime(2026, 6, 5).timestamp(), 83.12)])

    rc = reminders_tick(config, now=datetime(2026, 6, 16).timestamp(),
                        sender=collect_sender([]))

    assert rc == 0
