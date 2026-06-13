"""Tests for owner-authored scheduled jobs: the store, the tick, the gates.

The tick may launch a pre-recorded, owner-authored job — never compose one,
never start a conversation. Everything here runs on fakes: no real claude,
no network, no subprocesses.
"""

from __future__ import annotations

import json
import time

import pytest

from iris.config import Config
from iris.jobs import JobStore
from iris.schedules import ScheduleStore, add_rule, tick_schedules
from iris.usage import UsageLedger, month_key

NOW = 1_780_000_000.0  # an arbitrary fixed instant


def make_config(tmp_path, **overrides):
    fields = dict(
        scheduled_jobs_enabled=True,
        jobs_enabled=True,
        schedules_file=str(tmp_path / "sched.json"),
        jobs_file=str(tmp_path / "jobs.json"),
        workspaces_file=str(tmp_path / "ws.json"),
        inbox_file=str(tmp_path / "inbox.json"),
        usage_file=str(tmp_path / "usage.json"),
        job_grants=["shell"],
        jobs_max=2,
        home_channel="home-1",
    )
    fields.update(overrides)
    return Config(**fields)


def job_rule(store, config, *, title="briefing", when="2026-01-01T00:00:00Z",
             every="every 1d", instructions="write the morning briefing",
             grants="", cap=None):
    return add_rule(
        store, title=title, when=when, every=every, instructions=instructions,
        grants=grants, cap=cap, default_cap=config.schedule_monthly_cap,
    )


# -- store and validation ------------------------------------------------------


def test_add_rule_round_trip(tmp_path):
    config = make_config(tmp_path)
    store = ScheduleStore(config.schedules_file)
    rule = job_rule(store, config)
    assert rule["id"] == 1
    assert store.all()[0]["title"] == "briefing"
    assert store.all()[0]["enabled"] is True
    assert store.remove(1) is True
    assert store.all() == []


def test_add_rule_requires_exactly_one_payload(tmp_path):
    config = make_config(tmp_path)
    store = ScheduleStore(config.schedules_file)
    with pytest.raises(ValueError):
        add_rule(store, title="t", when="+1h", instructions="", command="")
    with pytest.raises(ValueError):
        add_rule(store, title="t", when="+1h", instructions="do", command="ls")


def test_add_rule_validates_grants_and_when(tmp_path):
    config = make_config(tmp_path)
    store = ScheduleStore(config.schedules_file)
    with pytest.raises(ValueError):
        add_rule(store, title="t", when="not a time", instructions="do")
    with pytest.raises(ValueError):
        add_rule(store, title="t", when="+1h", instructions="do", grants="sudo")


# -- the tick ------------------------------------------------------------------


def test_tick_is_off_by_default(tmp_path):
    config = make_config(tmp_path, scheduled_jobs_enabled=False)
    store = ScheduleStore(config.schedules_file)
    job_rule(store, config, when="2020-01-01T00:00:00Z")
    spawned = []
    out = tick_schedules(config, now=NOW, spawn=lambda jid, **kw: spawned.append(jid))
    assert "off" in out
    assert spawned == []
    assert JobStore(config.jobs_file).all() == []


def test_tick_launches_a_due_job_rule(tmp_path):
    config = make_config(tmp_path)
    store = ScheduleStore(config.schedules_file)
    rule = job_rule(store, config, grants="shell")
    spawned = []
    out = tick_schedules(config, now=NOW, spawn=lambda jid, **kw: spawned.append(jid))
    assert "1 launched" in out
    assert spawned == [1]
    job = JobStore(config.jobs_file).get(1)
    assert job["title"] == "[scheduled] briefing"
    assert job["instructions"] == "write the morning briefing"
    assert job["grants"] == ["subagents", "shell"]  # clamped to the ceiling
    fresh = store.get(rule["id"])
    assert fresh["last_job_id"] == 1
    assert fresh["due_ts"] == NOW + 86400  # rescheduled from the tick moment
    assert fresh["fired"] == {month_key(NOW): 1}


def test_tick_skips_a_rule_not_yet_due(tmp_path):
    config = make_config(tmp_path)
    store = ScheduleStore(config.schedules_file)
    job_rule(store, config, when="2099-01-01T00:00:00Z")
    out = tick_schedules(config, now=NOW, spawn=lambda jid, **kw: None)
    assert "0 due" in out
    assert JobStore(config.jobs_file).all() == []


def test_one_shot_rule_disables_after_firing(tmp_path):
    config = make_config(tmp_path)
    store = ScheduleStore(config.schedules_file)
    rule = job_rule(store, config, every="")
    tick_schedules(config, now=NOW, spawn=lambda jid, **kw: None)
    assert store.get(rule["id"])["enabled"] is False
    # a later tick does not fire it again
    out = tick_schedules(config, now=NOW + 60, spawn=lambda jid, **kw: None)
    assert "0 due" in out


def test_tick_enforces_the_monthly_cap(tmp_path):
    config = make_config(tmp_path)
    store = ScheduleStore(config.schedules_file)
    rule = job_rule(store, config, cap=2)
    store.update(rule["id"], fired={month_key(NOW): 2})
    spawned = []
    out = tick_schedules(config, now=NOW, spawn=lambda jid, **kw: spawned.append(jid))
    assert spawned == []
    assert "cap" in out
    assert JobStore(config.jobs_file).all() == []
    # still rescheduled, so it resumes next month instead of piling up
    assert store.get(rule["id"])["due_ts"] == NOW + 86400


def test_tick_skips_while_the_last_job_is_still_active(tmp_path):
    config = make_config(tmp_path)
    store = ScheduleStore(config.schedules_file)
    rule = job_rule(store, config)
    jstore = JobStore(config.jobs_file)
    prior = jstore.add("[scheduled] briefing", "i", ["subagents"], "", "home-1",
                       state="running")
    jstore.update(prior["id"], pid=__import__("os").getpid())  # genuinely alive
    store.update(rule["id"], last_job_id=prior["id"])
    spawned = []
    out = tick_schedules(config, now=NOW, spawn=lambda jid, **kw: spawned.append(jid))
    assert spawned == []
    assert "overlap" in out or "active" in out
    assert len(jstore.all()) == 1  # no second job was recorded


def test_tick_parks_when_the_credit_guard_is_hot(tmp_path):
    config = make_config(tmp_path, usage_budget_usd=10.0, usage_park_at=95.0)

    class Turn:
        cost_usd = 9.9
        context_tokens = 0

    UsageLedger(config.usage_file).record("chat", Turn())
    store = ScheduleStore(config.schedules_file)
    job_rule(store, config)
    spawned = []
    tick_schedules(config, now=time.time(), spawn=lambda jid, **kw: spawned.append(jid))
    assert spawned == []
    job = JobStore(config.jobs_file).get(1)
    assert job["state"] == "parked"


def test_job_rules_refuse_when_jobs_are_disabled(tmp_path):
    config = make_config(tmp_path, jobs_enabled=False)
    store = ScheduleStore(config.schedules_file)
    job_rule(store, config)
    spawned = []
    out = tick_schedules(config, now=NOW, spawn=lambda jid, **kw: spawned.append(jid))
    assert spawned == []
    assert "IRIS_JOBS" in out
    assert JobStore(config.jobs_file).all() == []


def test_script_rule_spawns_a_detached_watch(tmp_path):
    config = make_config(tmp_path)
    store = ScheduleStore(config.schedules_file)
    add_rule(store, title="backup", when="2020-01-01T00:00:00Z",
             every="every 1d", command="tar czf /tmp/b.tgz notes/")
    calls = []

    def fake_popen(argv, **kwargs):
        calls.append((list(argv), kwargs))
        class P:
            pid = 4242
        return P()

    out = tick_schedules(config, now=NOW, spawn=lambda jid, **kw: None, popen=fake_popen)
    assert "1 launched" in out
    assert len(calls) == 1
    argv = calls[0][0]
    assert "watch" in argv and "tar czf /tmp/b.tgz notes/" in argv
    assert calls[0][1].get("start_new_session") is True
    assert JobStore(config.jobs_file).all() == []  # script mode records no job


def test_fired_counts_drop_old_months(tmp_path):
    config = make_config(tmp_path)
    store = ScheduleStore(config.schedules_file)
    rule = job_rule(store, config)
    store.update(rule["id"], fired={"2020-01": 30})
    tick_schedules(config, now=NOW, spawn=lambda jid, **kw: None)
    assert store.get(rule["id"])["fired"] == {month_key(NOW): 1}


# -- review hardening ----------------------------------------------------------


def test_tick_repairs_a_dead_runner_before_the_overlap_check(tmp_path):
    # A runner killed by a reboot leaves its job 'running' with a dead pid; the
    # rule must repair it and fire, not stay bricked forever.
    config = make_config(tmp_path)
    store = ScheduleStore(config.schedules_file)
    rule = job_rule(store, config)
    jstore = JobStore(config.jobs_file)
    prior = jstore.add("[scheduled] briefing", "i", ["subagents"], "", "home-1",
                       state="running")
    jstore.update(prior["id"], pid=999999999)  # certainly dead
    store.update(rule["id"], last_job_id=prior["id"])
    spawned = []
    out = tick_schedules(config, now=NOW, spawn=lambda jid, **kw: spawned.append(jid))
    assert spawned, out
    assert jstore.get(prior["id"])["state"] == "failed"


def test_tick_replaces_a_stale_parked_firing(tmp_path):
    # A firing parked in a hot month must not wedge the rule once the month
    # cools: the stale parked clone is cancelled and a fresh launch happens.
    config = make_config(tmp_path)
    store = ScheduleStore(config.schedules_file)
    rule = job_rule(store, config)
    jstore = JobStore(config.jobs_file)
    prior = jstore.add("[scheduled] briefing", "i", ["subagents"], "", "home-1",
                       state="parked")
    store.update(rule["id"], last_job_id=prior["id"])
    spawned = []
    out = tick_schedules(config, now=NOW, spawn=lambda jid, **kw: spawned.append(jid))
    assert spawned, out
    assert jstore.get(prior["id"])["state"] == "cancelled"
    assert store.get(rule["id"])["last_job_id"] == spawned[0]


def test_tick_still_skips_a_genuinely_running_prior(tmp_path):
    config = make_config(tmp_path)
    store = ScheduleStore(config.schedules_file)
    rule = job_rule(store, config)
    jstore = JobStore(config.jobs_file)
    prior = jstore.add("[scheduled] briefing", "i", ["subagents"], "", "home-1",
                       state="running")
    jstore.update(prior["id"], pid=__import__("os").getpid())  # alive
    store.update(rule["id"], last_job_id=prior["id"])
    spawned = []
    out = tick_schedules(config, now=NOW, spawn=lambda jid, **kw: spawned.append(jid))
    assert spawned == []
    assert "overlap" in out or "still" in out


def test_parked_firing_does_not_consume_the_monthly_cap(tmp_path):
    config = make_config(tmp_path, usage_budget_usd=10.0, usage_park_at=95.0)

    class Turn:
        cost_usd = 9.9
        context_tokens = 0

    UsageLedger(config.usage_file).record("chat", Turn())
    store = ScheduleStore(config.schedules_file)
    rule = job_rule(store, config)
    tick_schedules(config, now=time.time(), spawn=lambda jid, **kw: None)
    fresh = store.get(rule["id"])
    assert fresh["fired"] == {}  # no model call happened; no cap slot burned
    assert fresh["last_job_id"] == 1  # but the parked clone is tracked


def test_parked_firing_leaves_an_inbox_note(tmp_path):
    from iris.inbox import Inbox

    config = make_config(tmp_path, usage_budget_usd=10.0, usage_park_at=95.0)

    class Turn:
        cost_usd = 9.9
        context_tokens = 0

    UsageLedger(config.usage_file).record("chat", Turn())
    store = ScheduleStore(config.schedules_file)
    job_rule(store, config)
    tick_schedules(config, now=time.time(), spawn=lambda jid, **kw: None)
    notes = Inbox(config.inbox_file).drain("discord:home-1")
    assert any("parked" in n.lower() for n in notes)


def test_script_rules_get_a_pid_overlap_guard(tmp_path):
    import os as _os

    config = make_config(tmp_path)
    store = ScheduleStore(config.schedules_file)
    rule = add_rule(store, title="backup", when="2020-01-01T00:00:00Z",
                    every="every 1d", command="sleep 999")
    calls = []

    class P:
        pid = _os.getpid()  # alive: the next firing must skip

    tick_schedules(config, now=NOW, spawn=lambda jid, **kw: None,
                   popen=lambda argv, **kw: calls.append(argv) or P())
    assert len(calls) == 1
    assert store.get(rule["id"])["last_script_pid"] == _os.getpid()
    out = tick_schedules(config, now=NOW + 86401, spawn=lambda jid, **kw: None,
                         popen=lambda argv, **kw: calls.append(argv) or P())
    assert len(calls) == 1  # second firing skipped: the first is still running
    assert "still" in out or "overlap" in out


def test_take_due_tolerates_malformed_entries(tmp_path):
    config = make_config(tmp_path)
    store = ScheduleStore(config.schedules_file)
    rule = job_rule(store, config)
    raw = json.loads(open(config.schedules_file, encoding="utf-8").read())
    raw.append("not a dict")
    raw.append({"id": 99, "title": "broken", "due_ts": None, "enabled": True,
                "instructions": "x"})
    open(config.schedules_file, "w", encoding="utf-8").write(json.dumps(raw))
    spawned = []
    tick_schedules(config, now=NOW, spawn=lambda jid, **kw: spawned.append(jid))
    assert spawned == [1]  # the good rule still fired
    assert store.get(rule["id"])["fired"]  # and was accounted


def test_load_quarantines_a_non_list_store(tmp_path):
    config = make_config(tmp_path)
    path = tmp_path / "sched.json"
    path.write_text('{"oops": "a dict"}', encoding="utf-8")
    store = ScheduleStore(path)
    assert store.all() == []
    # the original content was quarantined, not silently overwritten
    leftovers = list(tmp_path.glob("sched.json.corrupt*"))
    assert leftovers, "expected the malformed store to be preserved as .corrupt"


def test_corrupt_cap_on_one_rule_does_not_kill_the_batch(tmp_path):
    config = make_config(tmp_path)
    store = ScheduleStore(config.schedules_file)
    bad = job_rule(store, config, title="bad")
    good = job_rule(store, config, title="good")
    store.update(bad["id"], monthly_cap="oops")
    spawned = []
    out = tick_schedules(config, now=NOW, spawn=lambda jid, **kw: spawned.append(jid))
    assert len(spawned) == 1, out  # good still fired
    assert store.get(good["id"])["fired"]


def test_update_if_refuses_a_recreated_rule(tmp_path):
    config = make_config(tmp_path)
    store = ScheduleStore(config.schedules_file)
    rule = job_rule(store, config)
    store.remove(rule["id"])
    fresh = job_rule(store, config, title="recreated")  # may reuse the id
    assert store.update_if(fresh["id"], rule["created_ts"] - 1, fired={"x": 9}) is None
    assert store.get(fresh["id"]).get("fired") == {}


def test_describe_rule_reports_only_the_current_month(tmp_path):
    from iris.schedules import describe_rule

    config = make_config(tmp_path)
    store = ScheduleStore(config.schedules_file)
    rule = job_rule(store, config)
    store.update(rule["id"], fired={"2020-01": 30})
    line = describe_rule(store.get(rule["id"]), now=NOW)
    assert "fired 0 this month" in line


# -- review: don't consume a one-shot rule that never actually ran -------------


def test_oneshot_job_rule_survives_a_skip_when_jobs_disabled(tmp_path):
    config = make_config(tmp_path, jobs_enabled=False)  # scheduled on, jobs off
    store = ScheduleStore(config.schedules_file)
    rule = job_rule(store, config, every="")  # one-shot
    tick_schedules(config, now=NOW, spawn=lambda jid, **kw: None)
    fresh = store.get(rule["id"])
    assert fresh["enabled"] is True, "a one-shot that could not run must not be consumed"
    assert fresh.get("fired", {}) == {}


def test_oneshot_job_rule_is_consumed_once_it_actually_starts(tmp_path):
    config = make_config(tmp_path)
    store = ScheduleStore(config.schedules_file)
    rule = job_rule(store, config, every="")
    spawned = []
    tick_schedules(config, now=NOW, spawn=lambda jid, **kw: spawned.append(jid))
    assert spawned == [1]
    assert store.get(rule["id"])["enabled"] is False  # fired, so consumed


def test_describe_rule_tolerates_a_row_missing_id_or_title(tmp_path):
    from iris.schedules import describe_rule
    # a hand-edited row missing keys must not raise
    line = describe_rule({"due_ts": 0, "monthly_cap": 5})
    assert "#" in line
