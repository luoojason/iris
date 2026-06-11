"""Tests for the terminal job console (iris/jobs_console.py)."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from iris.config import Config
from iris.jobs import JobStore
from iris.workspaces import WorkspaceStore


def env(tmp_path, **over):
    cfg = dict(
        jobs_enabled=True,
        jobs_file=str(tmp_path / "jobs.json"),
        workspaces_file=str(tmp_path / "ws.json"),
        inbox_file=str(tmp_path / "inbox.json"),
        usage_file=str(tmp_path / "usage.json"),
        job_grants=["files"],
        jobs_max=2,
        jobs_keep=50,
        home_channel="home-1",
        discord_token="tok",
    )
    cfg.update(over)
    return Config(**cfg)


def args(action, **kw):
    base = dict(
        jobs_action=action, job_id=None, title=None, instructions=None,
        grant="", workspace="", keep=None, tui=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def run(config, a, spawned=None, files=None):
    import iris.jobs_console as console

    spawned = spawned if spawned is not None else []
    files = files if files is not None else []
    return console.jobs_command(
        config, a,
        spawn=lambda job_id, **k: spawned.append(job_id),
        send_file=lambda channel, path, text, token: files.append((channel, path)) or {"ok": True},
    )


# -- list / show -------------------------------------------------------------


def test_list_empty(tmp_path, capsys):
    rc = run(env(tmp_path), args("list"))
    assert rc == 0
    assert "no jobs" in capsys.readouterr().out.lower()


def test_list_shows_jobs_newest_first(tmp_path, capsys):
    config = env(tmp_path)
    store = JobStore(config.jobs_file)
    store.add("first audit", "x", ["subagents"], "", "home-1")
    store.add("second build", "y", ["subagents", "files"], "repo", "home-1")
    rc = run(config, args("list"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "first audit" in out and "second build" in out
    # newest first: #2 appears before #1
    assert out.index("#2") < out.index("#1")
    assert "pending" in out


def test_show_detail(tmp_path, capsys):
    config = env(tmp_path)
    store = JobStore(config.jobs_file)
    store.add("audit", "look at the repo", ["subagents"], "repo", "home-1")
    store.transition(1, ("pending",), "done", report="all clean", artifacts=["out.md"])
    rc = run(config, args("show", job_id=1))
    out = capsys.readouterr().out
    assert rc == 0
    assert "look at the repo" in out and "all clean" in out and "out.md" in out


def test_show_unknown_job(tmp_path, capsys):
    rc = run(env(tmp_path), args("show", job_id=99))
    assert rc == 1
    assert "no job #99" in capsys.readouterr().out.lower()


# -- run (hand-authored) -----------------------------------------------------


def test_run_creates_and_spawns(tmp_path, capsys):
    config = env(tmp_path)
    spawned = []
    rc = run(config, args("run", title="audit", instructions="check it"), spawned=spawned)
    out = capsys.readouterr().out
    assert rc == 0
    assert spawned == [1]
    job = JobStore(config.jobs_file).get(1)
    assert job["title"] == "audit" and job["instructions"] == "check it"
    assert job["channel_id"] == "home-1"


def test_run_clamps_grants_to_ceiling(tmp_path, capsys):
    config = env(tmp_path, job_grants=["files"])
    rc = run(config, args("run", title="t", instructions="i", grant="shell,files"))
    out = capsys.readouterr().out
    assert rc == 0
    assert JobStore(config.jobs_file).get(1)["grants"] == ["subagents", "files"]
    assert "shell" in out and ("refus" in out.lower() or "clamp" in out.lower())


def test_run_rejects_unknown_grant(tmp_path, capsys):
    config = env(tmp_path)
    rc = run(config, args("run", title="t", instructions="i", grant="root"))
    assert rc == 2
    assert "unknown grant" in capsys.readouterr().out.lower()
    assert JobStore(config.jobs_file).all() == []


def test_run_rejects_unknown_workspace(tmp_path, capsys):
    config = env(tmp_path)
    rc = run(config, args("run", title="t", instructions="i", workspace="nope"))
    assert rc == 2
    assert "workspace" in capsys.readouterr().out.lower()
    assert JobStore(config.jobs_file).all() == []


def test_run_accepts_registered_workspace(tmp_path):
    config = env(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    WorkspaceStore(config.workspaces_file).add("repo", str(repo))
    rc = run(config, args("run", title="t", instructions="i", workspace="repo"))
    assert rc == 0
    assert JobStore(config.jobs_file).get(1)["workspace"] == "repo"


def test_run_requires_title_and_instructions(tmp_path, capsys):
    rc = run(env(tmp_path), args("run", title="", instructions="i"))
    assert rc == 2
    assert "title" in capsys.readouterr().out.lower()


def test_run_parks_at_credit_park_level(tmp_path, capsys):
    from iris.usage import UsageLedger
    from iris.driver import ClaudeResult

    config = env(tmp_path, usage_budget_usd=10.0)
    UsageLedger(config.usage_file).record(
        "chat", ClaudeResult(text="", session_id=None, is_error=False, cost_usd=9.9))
    spawned = []
    rc = run(config, args("run", title="big", instructions="work"), spawned=spawned)
    out = capsys.readouterr().out
    assert rc == 0
    assert "park" in out.lower()
    assert spawned == []
    assert JobStore(config.jobs_file).get(1)["state"] == "parked"


def test_run_disabled_when_jobs_off(tmp_path, capsys):
    config = env(tmp_path, jobs_enabled=False)
    rc = run(config, args("run", title="t", instructions="i"))
    assert rc == 1
    assert "disabled" in capsys.readouterr().out.lower()


# -- actions: cancel / resume / rerun ----------------------------------------


def test_cancel_pending(tmp_path, capsys):
    config = env(tmp_path)
    JobStore(config.jobs_file).add("a", "x", [], "", "home-1")
    rc = run(config, args("cancel", job_id=1))
    assert rc == 0
    assert JobStore(config.jobs_file).get(1)["state"] == "cancelled"


def test_cancel_refuses_when_job_already_terminal(tmp_path, capsys):
    config = env(tmp_path)
    store = JobStore(config.jobs_file)
    store.add("a", "x", [], "", "home-1")
    store.transition(1, ("pending",), "done")
    rc = run(config, args("cancel", job_id=1))
    out = capsys.readouterr().out
    assert "already done" in out  # refused, reported truthfully


def test_resume_parked(tmp_path):
    config = env(tmp_path)
    store = JobStore(config.jobs_file)
    store.add("a", "x", [], "", "home-1")
    store.transition(1, ("pending",), "parked")
    spawned = []
    rc = run(config, args("resume", job_id=1), spawned=spawned)
    assert rc == 0 and spawned == [1]
    assert JobStore(config.jobs_file).get(1)["state"] == "pending"


def test_rerun_clones_and_spawns(tmp_path, capsys):
    config = env(tmp_path)
    store = JobStore(config.jobs_file)
    store.add("audit", "look hard", ["subagents"], "", "home-1")
    store.transition(1, ("pending",), "done", report="old")
    spawned = []
    rc = run(config, args("rerun", job_id=1), spawned=spawned)
    out = capsys.readouterr().out
    assert rc == 0
    assert spawned == [2]  # a new job id
    clone = JobStore(config.jobs_file).get(2)
    assert clone["instructions"] == "look hard" and clone["report"] == ""


def test_rerun_unknown(tmp_path, capsys):
    rc = run(env(tmp_path), args("rerun", job_id=99))
    assert rc == 1
    assert "no job #99" in capsys.readouterr().out.lower()


# -- artifacts / deliver -----------------------------------------------------


def test_artifacts_lists_names(tmp_path, capsys):
    config = env(tmp_path)
    store = JobStore(config.jobs_file)
    store.add("a", "x", [], "repo", "home-1")
    store.transition(1, ("pending",), "done", artifacts=["out/report.md", "clip.mp4"])
    rc = run(config, args("artifacts", job_id=1))
    out = capsys.readouterr().out
    assert rc == 0 and "out/report.md" in out and "clip.mp4" in out


def test_deliver_reuploads_artifacts(tmp_path, capsys):
    config = env(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "report.md").write_text("done", encoding="utf-8")
    WorkspaceStore(config.workspaces_file).add("repo", str(repo))
    store = JobStore(config.jobs_file)
    store.add("a", "x", [], "repo", "home-1")
    store.transition(1, ("pending",), "done", artifacts=["report.md"])
    files = []
    rc = run(config, args("deliver", job_id=1), files=files)
    assert rc == 0
    assert files == [("home-1", str((repo / "report.md").resolve()))]


def test_deliver_reports_missing_artifact(tmp_path, capsys):
    config = env(tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    WorkspaceStore(config.workspaces_file).add("repo", str(repo))
    store = JobStore(config.jobs_file)
    store.add("a", "x", [], "repo", "home-1")
    store.transition(1, ("pending",), "done", artifacts=["gone.md"])
    files = []
    rc = run(config, args("deliver", job_id=1), files=files)
    out = capsys.readouterr().out
    assert files == []
    assert "gone.md" in out  # the failure is named, never silent


def test_deliver_no_artifacts(tmp_path, capsys):
    config = env(tmp_path)
    JobStore(config.jobs_file).add("a", "x", [], "", "home-1")
    rc = run(config, args("deliver", job_id=1))
    assert "no artifacts" in capsys.readouterr().out.lower()


# -- prune -------------------------------------------------------------------


def test_prune_reports_count(tmp_path, capsys):
    config = env(tmp_path)
    store = JobStore(config.jobs_file)
    for n in range(5):
        store.add(f"t{n}", "x", [], "", "home-1")
        store.transition(n + 1, ("pending",), "done")
    rc = run(config, args("prune", keep=2))
    out = capsys.readouterr().out
    assert rc == 0 and "3" in out  # 5 terminal, keep 2, dropped 3


# -- dispatch / gating -------------------------------------------------------


def test_list_works_even_with_jobs_disabled(tmp_path, capsys):
    # Read-only views should work without IRIS_JOBS; only launching needs it.
    config = env(tmp_path, jobs_enabled=False)
    JobStore(config.jobs_file).add("a", "x", [], "", "home-1")
    rc = run(config, args("list"))
    assert rc == 0 and "a" in capsys.readouterr().out


def test_no_action_prints_usage(tmp_path, capsys):
    rc = run(env(tmp_path), args(None))
    assert rc == 2
    assert "usage" in capsys.readouterr().out.lower()


# -- CLI integration ---------------------------------------------------------


def test_cli_jobs_list_smoke(tmp_path, monkeypatch, capsys):
    from iris.cli import main

    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IRIS_JOBS", "true")
    monkeypatch.setenv("IRIS_JOBS_FILE", str(tmp_path / "jobs.json"))
    JobStore(tmp_path / "jobs.json").add("hello", "x", [], "", "")
    assert main(["jobs", "list"]) == 0
    assert "hello" in capsys.readouterr().out
