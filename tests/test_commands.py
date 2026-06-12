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


def test_status_reports_busy_queue_and_jobs(tmp_path):
    config = Config(jobs_enabled=True, jobs_file=str(tmp_path / "jobs.json"))
    JobStore(config.jobs_file).add("j", "i", ["subagents"], "", "h", state="running")
    out = commands.render_status(config, busy=True, pending=2, session_turns=5)
    assert "writing" in out.lower() or "reply" in out.lower()
    assert "2" in out and "5" in out
    assert "1" in out  # one active job


def test_status_idle(tmp_path):
    out = commands.render_status(Config(), busy=False, pending=0, session_turns=0)
    assert "idle" in out.lower()


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
    )


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
