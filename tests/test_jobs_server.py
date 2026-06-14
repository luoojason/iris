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


def test_start_job_records_origin_channel_when_set(env, monkeypatch):
    # The driver sets IRIS_ORIGIN_CHANNEL to the thread the turn ran in, so the
    # job reports back to THAT thread instead of always the home channel.
    monkeypatch.setenv("IRIS_ORIGIN_CHANNEL", "thread-42")
    srv.start_job("audit", "look at things")
    assert env["store"].get(1)["channel_id"] == "thread-42"


def test_start_job_falls_back_to_home_channel_without_origin(env, monkeypatch):
    monkeypatch.delenv("IRIS_ORIGIN_CHANNEL", raising=False)
    srv.start_job("audit", "look at things")
    assert env["store"].get(1)["channel_id"] == "home-1"


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


def test_resume_a_waiting_job_with_an_answer(env):
    srv.start_job("a", "one")
    env["store"].transition(1, ("pending",), "needs_input", question="prod or staging?")
    reply = srv.resume_job(1, answer="use staging")
    assert "#1" in reply
    job = env["store"].get(1)
    assert job["state"] == "pending"  # re-queued to resume
    assert job["pending_answer"] == "use staging"  # the answer is recorded for the runner
    assert env["spawned"][-1] == 1


def test_resume_a_waiting_job_needs_an_answer(env):
    srv.start_job("a", "one")
    env["store"].transition(1, ("pending",), "needs_input", question="prod or staging?")
    before = len(env["spawned"])
    reply = srv.resume_job(1)  # no answer
    assert "answer" in reply.lower()
    assert env["store"].get(1)["state"] == "needs_input"  # still waiting
    assert len(env["spawned"]) == before  # not re-launched


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


def test_schedule_job_caps_model_created_rules(sched_env, monkeypatch):
    monkeypatch.setattr(srv, "MAX_MODEL_RULES", 2)
    srv.schedule_job("a", "i", when="+1h")
    srv.schedule_job("b", "i", when="+1h")
    out = srv.schedule_job("c", "i", when="+1h")
    assert "cancel" in out.lower()
    from iris.schedules import ScheduleStore

    assert len(ScheduleStore(sched_env.schedules_file).all()) == 2


# -- run_in_background: long compute without a model-turn timeout ---------------


@pytest.fixture
def bg_env(tmp_path, monkeypatch):
    config = Config(jobs_enabled=True, job_grants=["shell"],
                    workspaces_file=str(tmp_path / "ws.json"), home_channel="home-1")
    monkeypatch.setattr(srv, "_CONFIG", config)
    calls = []
    monkeypatch.setattr(srv, "_launch_watch", lambda argv, cwd: calls.append((argv, cwd)))
    return {"config": config, "calls": calls, "tmp": tmp_path}


def test_run_in_background_spawns_a_detached_watch(bg_env):
    out = srv.run_in_background("build_video.sh xqc", label="build xqc")
    assert "background" in out.lower()
    argv = bg_env["calls"][0][0]
    assert "watch" in argv and argv[-1].endswith("build_video.sh xqc")
    assert argv[argv.index("--name") + 1] == "build xqc"


def test_run_in_background_needs_the_shell_grant(bg_env):
    bg_env["config"].job_grants = []
    out = srv.run_in_background("anything")
    assert "shell" in out.lower()
    assert bg_env["calls"] == []


def test_run_in_background_default_does_not_add_resume(bg_env):
    srv.run_in_background("build.sh", label="b")
    argv = bg_env["calls"][0][0]
    assert "--resume" not in argv  # plain background command: ping + fold only


def test_run_in_background_autoresume_adds_resume_flag(bg_env):
    bg_env["config"].auto_resume = True
    out = srv.run_in_background("build.sh", label="b", autoresume=True)
    argv = bg_env["calls"][0][0]
    assert "--resume" in argv
    assert "continue" in out.lower() or "carry" in out.lower()


def test_run_in_background_autoresume_honest_when_master_flag_off(bg_env):
    bg_env["config"].auto_resume = False
    out = srv.run_in_background("build.sh", label="b", autoresume=True)
    argv = bg_env["calls"][0][0]
    assert "--resume" not in argv  # inert: do not enqueue when the owner has it off
    assert "off" in out.lower() or "ping" in out.lower()


def test_run_in_background_autoresume_honest_without_a_home_channel(bg_env):
    # Master flag on but no home channel: watch would have nowhere to resume, so
    # the reply must not promise self-continuation and the flag must not be set.
    bg_env["config"].auto_resume = True
    bg_env["config"].home_channel = ""
    out = srv.run_in_background("build.sh", label="b", autoresume=True)
    argv = bg_env["calls"][0][0]
    assert "--resume" not in argv
    assert "off" in out.lower() or "ping" in out.lower()


def test_run_in_background_gated_on_jobs(bg_env):
    bg_env["config"].jobs_enabled = False
    out = srv.run_in_background("anything")
    assert "disabled" in out.lower()
    assert bg_env["calls"] == []


def test_run_in_background_runs_in_a_named_workspace(bg_env):
    from iris.workspaces import WorkspaceStore
    WorkspaceStore(bg_env["config"].workspaces_file).add("clipper", str(bg_env["tmp"]))
    srv.run_in_background("./build_video.sh xqc", workspace="clipper")
    inner = bg_env["calls"][0][0][-1]
    assert inner.startswith("cd ") and "build_video.sh xqc" in inner
    assert str(bg_env["tmp"]) in inner


def test_run_in_background_unknown_workspace_refuses(bg_env):
    out = srv.run_in_background("x", workspace="nope")
    assert "No workspace" in out
    assert bg_env["calls"] == []


def test_run_in_background_empty_command_refuses(bg_env):
    out = srv.run_in_background("   ")
    assert bg_env["calls"] == []


def test_run_in_background_asks_watch_to_fold_into_context(bg_env):
    srv.run_in_background("build_video.sh xqc", label="bx")
    argv = bg_env["calls"][0][0]
    assert "--fold" in argv  # so the completion folds into Iris's next turn
