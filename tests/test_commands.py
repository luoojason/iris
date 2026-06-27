"""Tests for the bang-command control plane (iris/commands.py).

These commands are intercepted before any model turn, so the whole point is
that they make ZERO model calls: every renderer reads state files or acts on a
process, never the brain. Exercised here against temp stores and fakes.
"""

from __future__ import annotations

import pytest

from iris import commands
from iris.config import Config
from iris.jobs import JobStore
from iris.schedules import ScheduleStore


# -- parsing -------------------------------------------------------------------


def test_parse_bare_command():
    cmd = commands.parse("!usage")
    assert cmd is not None and cmd.name == "usage" and cmd.arg == ""


def test_parse_is_case_insensitive_and_trims():
    assert commands.parse("  !USAGE  ").name == "usage"


def test_parse_aliases_fold_to_canonical_names():
    assert commands.parse("!reset").name == "new"
    assert commands.parse("!forget").name == "new"
    assert commands.parse("!newchat").name == "new"
    assert commands.parse("!cancel 5").name == "stop"


def test_parse_non_command_text_is_none():
    assert commands.parse("hello there") is None
    assert commands.parse("") is None
    assert commands.parse("!") is None


def test_parse_unknown_bang_word_is_none():
    # An unknown !word must fall through to the agent, not be swallowed.
    assert commands.parse("!frobnicate") is None


def test_parse_does_not_hijack_prose_starting_with_a_command_word():
    # "!help me debug this" is a real message, not the !help command.
    assert commands.parse("!help me debug this") is None
    assert commands.parse("!new feature idea: ...") is None


def test_parse_stop_takes_an_optional_job_id():
    assert commands.parse("!stop").arg == ""
    assert commands.parse("!stop 7").arg == "7"
    assert commands.parse("!cancel 7").name == "stop"
    assert commands.parse("!cancel 7").arg == "7"


# -- renderers (zero model calls) ----------------------------------------------


def test_usage_renders_the_ledger_summary(tmp_path):
    config = Config(usage_file=str(tmp_path / "u.json"))
    out = commands.render_usage(config)
    assert "month:" in out  # summary_text shape, no model call


def test_jobs_lists_recent_jobs(tmp_path):
    import os

    config = Config(jobs_enabled=True, jobs_file=str(tmp_path / "jobs.json"))
    store = JobStore(config.jobs_file)
    store.add("audit the repo", "i", ["subagents"], "", "home-1", state="running")
    store.update(1, pid=os.getpid())  # a live pid so repair leaves it running
    out = commands.render_jobs(config)
    assert "#1" in out and "running" in out and "audit the repo" in out


def test_jobs_off_when_disabled(tmp_path):
    out = commands.render_jobs(Config(jobs_enabled=False))
    assert "off" in out.lower() and "IRIS_JOBS" in out


def test_schedules_lists_rules(tmp_path):
    config = Config(jobs_enabled=True, scheduled_jobs_enabled=True,
                    schedules_file=str(tmp_path / "s.json"))
    from iris.schedules import add_rule
    add_rule(ScheduleStore(config.schedules_file), title="briefing",
             when="2099-01-01T00:00:00Z", every="every 1d",
             instructions="write it")
    out = commands.render_schedules(config)
    assert "briefing" in out and "#1" in out


def test_schedules_off_when_disabled(tmp_path):
    out = commands.render_schedules(Config(scheduled_jobs_enabled=False))
    assert "off" in out.lower() and "IRIS_SCHEDULED_JOBS" in out


def test_parse_goals_and_heartbeat_are_no_arg_commands():
    assert commands.parse("!goals").name == "goals"
    assert commands.parse("!heartbeat").name == "heartbeat"
    assert commands.parse("!goals are important to me") is None  # prose, not swallowed


def test_goals_lists_active_goals(tmp_path):
    from iris.goals import GoalStore

    config = Config(goals_file=str(tmp_path / "g.json"))
    GoalStore(config.goals_file).add("ship the roadmap", now=1.0)
    out = commands.render_goals(config)
    assert "ship the roadmap" in out and "#1" in out


def test_goals_when_none(tmp_path):
    out = commands.render_goals(Config(goals_file=str(tmp_path / "g.json")))
    assert "no goals" in out.lower()


def test_heartbeat_reports_last_known_status_without_probing(tmp_path):
    import json

    config = Config(heartbeat_file=str(tmp_path / "hb.json"),
                    heartbeat_state=str(tmp_path / "hb.state.json"))
    (tmp_path / "hb.json").write_text(json.dumps([
        {"name": "site", "kind": "url_ok", "url": "https://e.com"},
        {"name": "disk", "kind": "disk_free", "path": "/", "min_percent": 10}]), "utf-8")
    # a prior tick recorded 'site' as failing; !heartbeat reads that, no fresh probe
    (tmp_path / "hb.state.json").write_text(json.dumps({"failing": ["site"]}), "utf-8")
    out = commands.render_heartbeat(config)
    assert "site" in out and "failing" in out.lower()


def test_heartbeat_all_clear_and_no_file(tmp_path):
    import json

    cfg = Config(heartbeat_file=str(tmp_path / "hb.json"),
                 heartbeat_state=str(tmp_path / "hb.state.json"))
    (tmp_path / "hb.json").write_text(json.dumps([
        {"name": "site", "kind": "url_ok", "url": "https://e.com"}]), "utf-8")
    assert "clear" in commands.render_heartbeat(cfg).lower()  # no failing set
    assert "no heartbeat" in commands.render_heartbeat(Config(
        heartbeat_file=str(tmp_path / "absent.json"))).lower()


def test_status_reports_busy_queue_and_jobs(tmp_path):
    import os

    config = Config(jobs_enabled=True, jobs_file=str(tmp_path / "jobs.json"))
    store = JobStore(config.jobs_file)
    store.add("j", "i", ["subagents"], "", "h", state="running")
    store.update(1, pid=os.getpid())  # a live pid so repair leaves it active
    out = commands.render_status(config, busy=True, pending=2, session_turns=5)
    assert "writing" in out.lower() or "reply" in out.lower()
    assert "2" in out and "5" in out
    assert "1" in out  # one active job


def test_status_idle(tmp_path):
    out = commands.render_status(Config(), busy=False, pending=0, session_turns=0)
    assert "idle" in out.lower()


def test_status_flags_jobs_waiting_on_an_answer(tmp_path):
    config = Config(jobs_enabled=True, jobs_file=str(tmp_path / "jobs.json"))
    store = JobStore(config.jobs_file)
    store.add("j", "i", ["subagents"], "", "h")
    store.transition(1, ("pending",), "needs_input", question="prod or staging?")
    out = commands.render_status(config, busy=False, pending=0, session_turns=0)
    assert "waiting on your answer" in out.lower()


def test_cancel_job_invokes_the_real_canceller(tmp_path):
    config = Config(jobs_enabled=True, jobs_file=str(tmp_path / "jobs.json"))
    store = JobStore(config.jobs_file)
    store.add("j", "i", ["subagents"], "", "h", state="pending")
    out = commands.cancel_job(config, "1")
    assert "#1" in out
    assert store.get(1)["state"] == "cancelled"


def test_cancel_job_rejects_a_non_number(tmp_path):
    out = commands.cancel_job(Config(jobs_enabled=True, jobs_file=str(tmp_path / "j.json")), "abc")
    assert "abc" in out


def test_cancel_job_off_when_disabled():
    assert "off" in commands.cancel_job(Config(jobs_enabled=False), "1").lower()


def test_help_lists_the_commands():
    out = commands.HELP
    for c in ("!usage", "!jobs", "!stop", "!new", "!schedules", "!status"):
        assert c in out


# -- dispatch (wires renderers + injected side effects) ------------------------


def _dispatch(cmd_text, config, **kw):
    cmd = commands.parse(cmd_text)
    assert cmd is not None
    return commands.dispatch(
        cmd, config,
        reset=kw.get("reset", lambda: None),
        stop=kw.get("stop", lambda: "Nothing is running here right now."),
        status_fields=kw.get("status_fields", lambda: {"busy": False, "pending": 0, "session_turns": 0}),
        set_footer=kw.get("set_footer"),
    )


def test_parse_footer_takes_on_off_or_nothing():
    assert commands.parse("!footer on").arg == "on"
    assert commands.parse("!footer off").arg == "off"
    assert commands.parse("!footer").arg == ""
    assert commands.parse("!footer please show it") is None  # arg is prose -> not a command


def test_parse_recap_is_a_no_arg_command():
    assert commands.parse("!recap").name == "recap"
    assert commands.parse("!recap the meeting") is None  # trailing prose -> not a command


def test_dispatch_footer_toggles_through_the_hook():
    flips = []

    def set_footer(want):
        flips.append(want)
        return f"footer {want}"

    assert _dispatch("!footer on", Config(), set_footer=set_footer) == "footer True"
    assert _dispatch("!footer off", Config(), set_footer=set_footer) == "footer False"
    assert _dispatch("!footer", Config(), set_footer=set_footer) == "footer None"  # report state
    assert flips == [True, False, None]


def test_dispatch_footer_without_hook_says_unavailable():
    out = _dispatch("!footer on", Config())  # no set_footer (e.g. a transport without it)
    assert "isn't available" in out


def test_dispatch_new_calls_reset(tmp_path):
    calls = []
    out = _dispatch("!new", Config(), reset=lambda: calls.append("reset"))
    assert calls == ["reset"]
    assert "fresh" in out.lower()


def test_dispatch_stop_no_arg_calls_stop(tmp_path):
    out = _dispatch("!stop", Config(), stop=lambda: "Stopped the current reply.")
    assert "Stopped" in out


def test_dispatch_stop_with_id_cancels_the_job(tmp_path):
    config = Config(jobs_enabled=True, jobs_file=str(tmp_path / "jobs.json"))
    JobStore(config.jobs_file).add("j", "i", ["subagents"], "", "h", state="pending")
    out = _dispatch("!stop 1", config)
    assert "#1" in out
    assert JobStore(config.jobs_file).get(1)["state"] == "cancelled"


def test_dispatch_usage(tmp_path):
    out = _dispatch("!usage", Config(usage_file=str(tmp_path / "u.json")))
    assert "month:" in out


# -- review hardening: stop-arg digit guard ------------------------------------


def test_stop_with_prose_falls_through_to_the_agent():
    # "!stop the war", "!cancel that order" are real messages, not job cancels.
    for msg in ("!stop now", "!stop it", "!stop the war", "!cancel that", "!stop please"):
        assert commands.parse(msg) is None, msg


def test_stop_accepts_only_plain_digits_as_a_job_id():
    assert commands.parse("!stop 7").arg == "7"
    assert commands.parse("!cancel 7").name == "stop" and commands.parse("!cancel 7").arg == "7"
    # signs, separators, non-ascii digits are not job ids -> fall through
    for bad in ("!stop -7", "!stop +7", "!stop 1_000", "!stop 7x"):
        assert commands.parse(bad) is None, bad


def test_bang_space_word_is_not_a_stop_command():
    assert commands.parse("! stop the war") is None


# -- review hardening: status repairs, jobs tolerate missing timestamps --------


def test_status_does_not_count_a_crashed_job_as_active(tmp_path):
    config = Config(jobs_enabled=True, jobs_file=str(tmp_path / "jobs.json"))
    store = JobStore(config.jobs_file)
    store.add("j", "i", ["subagents"], "", "h", state="running")  # no pid -> dead
    out = commands.render_status(config, busy=False, pending=0, session_turns=0)
    assert "0 background job(s) active" in out
    assert store.get(1)["state"] == "failed"  # repaired on the touch, like !jobs


def test_jobs_tolerates_a_row_with_no_timestamp(tmp_path):
    import json
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps([{"id": 1, "title": "x", "state": "pending"}]), encoding="utf-8")
    out = commands.render_jobs(Config(jobs_enabled=True, jobs_file=str(path)))
    assert "#1" in out  # must not raise on a missing created_ts
