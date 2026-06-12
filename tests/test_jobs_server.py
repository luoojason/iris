"""Tests for the jobs MCP server (iris/mcp/jobs.py)."""

from __future__ import annotations

import time

import pytest

import iris.mcp.jobs as srv
from iris.config import Config
from iris.jobs import JobStore
from iris.workspaces import WorkspaceStore


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point the server at temp stores and record spawns."""
    config = Config(
        jobs_enabled=True,
        jobs_file=str(tmp_path / "jobs.json"),
        workspaces_file=str(tmp_path / "ws.json"),
        job_grants=["files"],
        jobs_max=2,
        home_channel="home-1",
    )
    spawned = []
    monkeypatch.setattr(srv, "_CONFIG", config)
    monkeypatch.setattr(srv, "SPAWN", lambda job_id, **kw: spawned.append(job_id))
    return {
        "config": config,
        "spawned": spawned,
        "store": JobStore(config.jobs_file),
        "workspaces": WorkspaceStore(config.workspaces_file),
        "tmp": tmp_path,
    }


def test_start_job_is_gated_on_iris_jobs(env, monkeypatch):
    monkeypatch.setattr(srv, "_CONFIG", Config(jobs_enabled=False))
    reply = srv.start_job("t", "do it")
    assert "disabled" in reply
    assert env["spawned"] == []


def test_start_job_records_and_spawns(env):
    reply = srv.start_job("audit", "look at things")
    assert "Job #1" in reply and "started" in reply
    assert "subagents" in reply
    assert env["spawned"] == [1]
    job = env["store"].get(1)
    assert job["state"] == "pending"
    assert job["channel_id"] == "home-1"
    assert job["grants"] == ["subagents"]


def test_start_job_clamps_grants_to_the_ceiling(env):
    reply = srv.start_job("t", "i", grants="shell, files")
    assert "Refused grants" in reply and "shell" in reply
    assert env["store"].get(1)["grants"] == ["subagents", "files"]


def test_start_job_rejects_unknown_grants(env):
    reply = srv.start_job("t", "i", grants="sudo")
    assert "unknown grant" in reply
    assert env["store"].all() == []  # nothing recorded


def test_start_job_rejects_unknown_workspace_without_recording(env):
    reply = srv.start_job("t", "i", workspace="nope")
    assert "No workspace named 'nope'" in reply
    assert env["store"].all() == []
    assert env["spawned"] == []


def test_start_job_accepts_registered_workspace(env):
    repo = env["tmp"] / "repo"
    repo.mkdir()
    env["workspaces"].add("repo", str(repo))
    srv.start_job("t", "i", workspace="repo")
    assert env["store"].get(1)["workspace"] == "repo"


def test_start_job_requires_title_and_instructions(env):
    assert "needs both" in srv.start_job("", "i")
    assert "needs both" in srv.start_job("t", "  ")
    assert env["store"].all() == []


def test_start_job_queues_past_the_cap(env):
    srv.start_job("a", "one")
    srv.start_job("b", "two")
    reply = srv.start_job("c", "three")
    assert "queued" in reply and "resume_job(3)" in reply
    assert env["spawned"] == [1, 2]  # the third was not launched
    assert env["store"].get(3)["state"] == "pending"


def test_cancel_pending_job(env):
    srv.start_job("a", "one")
    reply = srv.cancel_job(1)
    assert "Cancelled job #1" in reply
    assert env["store"].get(1)["state"] == "cancelled"


def test_cancel_running_job_kills_the_runner(env, monkeypatch):
    srv.start_job("a", "one")
    env["store"].transition(1, ("pending",), "running", pid=4242, started_ts=time.time())
    killed = []
    monkeypatch.setattr(srv, "_kill_runner", lambda pid: killed.append(pid) or True)
    reply = srv.cancel_job(1)
    assert "Cancelled job #1." == reply
    assert killed == [4242]
    assert env["store"].get(1)["state"] == "cancelled"


def test_cancel_finished_job_says_so(env):
    srv.start_job("a", "one")
    env["store"].transition(1, ("pending",), "done")
    assert "already done" in srv.cancel_job(1)


def test_resume_parked_job_spawns(env):
    srv.start_job("a", "one")
    env["store"].transition(1, ("pending",), "parked")
    reply = srv.resume_job(1)
    assert "Resumed job #1" in reply
    assert env["spawned"][-1] == 1
    assert env["store"].get(1)["state"] == "pending"


def test_resume_refuses_terminal_states(env):
    srv.start_job("a", "one")
    env["store"].transition(1, ("pending",), "done")
    assert "only parked or queued" in srv.resume_job(1)


def test_job_status_and_list(env):
    srv.start_job("audit", "one")
    env["store"].transition(
        1, ("pending",), "done",
        report="all clean", artifacts=["out.md"],
        started_ts=time.time(), finished_ts=time.time(),
    )
    status = srv.job_status(1)
    assert "Job #1 (audit): done" in status
    assert "all clean" in status and "out.md" in status
    assert "No job #9." == srv.job_status(9)
    listing = srv.list_jobs()
    assert "#1 [done] audit" in listing


def test_list_repairs_dead_runners(env, monkeypatch):
    import iris.jobs as jobs_mod

    srv.start_job("a", "one")
    env["store"].transition(1, ("pending",), "running", pid=999999999)
    monkeypatch.setattr(jobs_mod, "_pid_alive", lambda pid: False)
    listing = srv.list_jobs()
    assert "[failed]" in listing
    assert env["store"].get(1)["error"] == "the job runner died"


def test_cancel_running_job_kills_both_process_groups(env, monkeypatch):
    """The claude child runs in its OWN session (driver hardening), so a
    cancel must kill the recorded claude pid too or the turn keeps burning
    credit and running tools after the owner said stop."""
    srv.start_job("a", "one")
    env["store"].transition(1, ("pending",), "running", pid=111, claude_pid=222,
                            started_ts=time.time())
    killed = []
    monkeypatch.setattr(srv, "_kill_runner", lambda pid: killed.append(pid) or True)
    reply = srv.cancel_job(1)
    assert "Cancelled job #1" in reply
    assert set(killed) == {111, 222}


def test_cancel_reports_truth_when_the_job_finished_first(env, monkeypatch):
    srv.start_job("a", "one")
    env["store"].transition(1, ("pending",), "running", pid=111)

    def kill_and_finish(pid):
        # while we were killing, the runner completed and recorded done
        env["store"].transition(1, ("running",), "done", report="won the race")
        return True

    monkeypatch.setattr(srv, "_kill_runner", kill_and_finish)
    reply = srv.cancel_job(1)
    assert "already done" in reply  # never claim a cancel that did not happen


def test_status_list_cancel_are_gated_on_iris_jobs(env, monkeypatch):
    srv.start_job("a", "one")
    monkeypatch.setattr(srv, "_CONFIG", Config(jobs_enabled=False,
                                               jobs_file=env["config"].jobs_file))
    assert "disabled" in srv.job_status(1)
    assert "disabled" in srv.list_jobs()
    assert "disabled" in srv.cancel_job(1)
    assert env["store"].get(1)["state"] == "pending"  # nothing was touched


def test_parked_reply_still_reports_clamped_grants(env, monkeypatch, tmp_path):
    from iris.usage import UsageLedger
    from iris.driver import ClaudeResult

    config = Config(
        jobs_enabled=True,
        jobs_file=env["config"].jobs_file,
        workspaces_file=env["config"].workspaces_file,
        job_grants=["files"],
        usage_file=str(tmp_path / "u.json"),
        usage_budget_usd=10.0,
    )
    UsageLedger(config.usage_file).record(
        "chat", ClaudeResult(text="", session_id=None, is_error=False, cost_usd=9.9))
    monkeypatch.setattr(srv, "_CONFIG", config)
    reply = srv.start_job("big", "work", grants="shell, files")
    assert "PARKED" in reply
    assert "Refused grants" in reply and "shell" in reply


# -- scheduled jobs ------------------------------------------------------------


@pytest.fixture
def sched_env(tmp_path, monkeypatch):
    config = Config(
        jobs_enabled=True,
        scheduled_jobs_enabled=True,
        jobs_file=str(tmp_path / "jobs.json"),
        schedules_file=str(tmp_path / "sched.json"),
        workspaces_file=str(tmp_path / "ws.json"),
        job_grants=["files"],
        home_channel="home-1",
    )
    monkeypatch.setattr(srv, "_CONFIG", config)
    return config


def test_schedule_job_records_a_model_rule(sched_env):
    from iris.schedules import ScheduleStore

    reply = srv.schedule_job("nightly check", "run the repo health check",
                             when="+1h", every="every 1d")
    assert "#1" in reply
    rule = ScheduleStore(sched_env.schedules_file).get(1)
    assert rule["created_by"] == "model"
    assert rule["instructions"] == "run the repo health check"
    assert rule["enabled"] is True


def test_schedule_job_is_gated_on_the_flag(sched_env, monkeypatch):
    sched_env.scheduled_jobs_enabled = False
    reply = srv.schedule_job("t", "i", when="+1h")
    assert "IRIS_SCHEDULED_JOBS" in reply
    from iris.schedules import ScheduleStore

    assert ScheduleStore(sched_env.schedules_file).all() == []


def test_schedule_job_never_takes_a_command(sched_env):
    # The model writes job instructions only; shell commands on a clock are
    # owner-CLI territory. The tool simply has no command parameter, and the
    # rule it creates must be a job rule.
    from iris.schedules import ScheduleStore

    srv.schedule_job("t", "instructions here", when="+1h")
    rule = ScheduleStore(sched_env.schedules_file).get(1)
    assert rule["command"] == ""


def test_list_and_cancel_schedules(sched_env):
    srv.schedule_job("nightly", "check things", when="+1h", every="every 1d")
    listing = srv.list_schedules()
    assert "nightly" in listing and "#1" in listing
    reply = srv.cancel_schedule(1)
    assert "Cancelled" in reply
    assert "No schedules" in srv.list_schedules()
