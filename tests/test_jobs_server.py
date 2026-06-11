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
    monkeypatch.setattr(srv, "SPAWN", lambda job_id: spawned.append(job_id))
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
