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
