"""Tests for the hybrid job coordinator (iris/jobs.py)."""

from __future__ import annotations

import os

import pytest

from iris.config import Config
from iris.driver import DANGEROUS_BUILTINS, ClaudeResult
from iris.inbox import Inbox
from iris.jobs import (
    GRANT_TOOLS,
    JobStore,
    clamp_grants,
    job_allowed_builtins,
    job_disallowed,
    parse_grants,
    run_job,
)
from iris.workspaces import WorkspaceStore


# -- grants ------------------------------------------------------------------


def test_parse_grants_validates_and_dedupes():
    assert parse_grants("") == []
    assert parse_grants("shell, files, shell") == ["shell", "files"]
    with pytest.raises(ValueError):
        parse_grants("shell, root")


def test_clamp_grants_always_includes_subagents():
    granted, clamped = clamp_grants([], [])
    assert granted == ["subagents"]
    assert clamped == []


def test_clamp_grants_enforces_the_ceiling():
    granted, clamped = clamp_grants(["shell", "files"], ["files"])
    assert granted == ["subagents", "files"]
    assert clamped == ["shell"]


def test_job_disallowed_is_derived_from_dangerous_builtins():
    """The job denylist must be a pure derivation of DANGEROUS_BUILTINS.

    An explicit disallowed_tools REPLACES the driver default, so anything
    hand-written here would silently stop tracking future additions.
    """
    unlocked = set(GRANT_TOOLS["subagents"])
    expected = tuple(t for t in DANGEROUS_BUILTINS if t not in unlocked)
    assert job_disallowed(["subagents"]) == expected

    unlocked |= set(GRANT_TOOLS["shell"])
    expected = tuple(t for t in DANGEROUS_BUILTINS if t not in unlocked)
    assert job_disallowed(["subagents", "shell"]) == expected


def test_job_denylist_always_keeps_ungranted_dangerous_tools():
    denied = job_disallowed(["subagents"])
    assert "Bash" in denied and "Write" in denied
    assert "Task" not in denied and "Agent" not in denied


def test_job_allowed_builtins_lists_granted_tools():
    assert job_allowed_builtins(["subagents", "files"]) == list(
        GRANT_TOOLS["subagents"] + GRANT_TOOLS["files"]
    )


# -- store -------------------------------------------------------------------


def make_store(tmp_path):
    return JobStore(tmp_path / "jobs.json")


def test_store_add_get_roundtrip(tmp_path):
    store = make_store(tmp_path)
    job = store.add("audit", "look at everything", ["subagents"], "", "chan-1")
    assert job["id"] == 1 and job["state"] == "pending"
    loaded = store.get(1)
    assert loaded["title"] == "audit"
    assert loaded["instructions"] == "look at everything"
    assert loaded["channel_id"] == "chan-1"


def test_store_ids_increment(tmp_path):
    store = make_store(tmp_path)
    assert store.add("a", "x", [], "", "")["id"] == 1
    assert store.add("b", "y", [], "", "")["id"] == 2


def test_transition_guards_the_source_state(tmp_path):
    store = make_store(tmp_path)
    store.add("a", "x", [], "", "")
    assert store.transition(1, ("pending",), "running") is not None
    # a second runner racing on the same job must lose
    assert store.transition(1, ("pending",), "running") is None
    assert store.get(1)["state"] == "running"


def test_count_active_counts_pending_and_running(tmp_path):
    store = make_store(tmp_path)
    store.add("a", "x", [], "", "")
    store.add("b", "y", [], "", "")
    store.transition(2, ("pending",), "running")
    store.add("c", "z", [], "", "")
    store.transition(3, ("pending",), "done")
    assert store.count_active() == 2


def test_corrupt_store_starts_fresh(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text("[broken", encoding="utf-8")
    assert JobStore(path).all() == []


# -- runner ------------------------------------------------------------------


class FakeJobDriver:
    def __init__(self, result):
        self.result = result
        self.prompts = []

    def run(self, prompt, session_id=None, model=None):
        self.prompts.append(prompt)
        return self.result


def runner_env(tmp_path, *, result, workspace=None, grants=None):
    """Build a job + every injectable seam, returning them as a dict."""
    store = make_store(tmp_path)
    ws_store = WorkspaceStore(tmp_path / "ws.json")
    ws_name = ""
    if workspace is not None:
        ws_store.add("ws", str(workspace))
        ws_name = "ws"
    job = store.add("the job", "do the thing", grants or ["subagents"], ws_name, "chan-9")
    inbox = Inbox(tmp_path / "inbox.json")
    driver = FakeJobDriver(result)
    captured = {}

    def driver_factory(config, job, workspace_path):
        captured["workspace_path"] = workspace_path
        captured["job"] = dict(job)
        return driver

    pings = []
    files = []
    config = Config(jobs_enabled=True, discord_token="tok")
    return {
        "store": store, "ws_store": ws_store, "inbox": inbox, "driver": driver,
        "driver_factory": driver_factory, "captured": captured, "config": config,
        "job_id": job["id"],
        "send_message": lambda channel, text, token: pings.append((channel, text)) or True,
        "send_file": lambda channel, path, text, token: files.append((channel, path)) or {"ok": True},
        "pings": pings, "files": files,
    }


def run_with(env):
    return run_job(
        env["job_id"], env["config"],
        store=env["store"], workspace_store=env["ws_store"], inbox=env["inbox"],
        driver_factory=env["driver_factory"],
        send_message=env["send_message"], send_file=env["send_file"],
    )


def test_run_job_happy_path(tmp_path):
    env = runner_env(tmp_path, result=ClaudeResult(text="all done", session_id="s", is_error=False))
    assert run_with(env) == 0
    job = env["store"].get(1)
    assert job["state"] == "done"
    assert job["report"] == "all done"
    assert job["started_ts"] and job["finished_ts"]
    assert env["driver"].prompts == ["do the thing"]
    assert len(env["pings"]) == 1
    channel, text = env["pings"][0]
    assert channel == "chan-9"
    assert "job #1" in text and "finished" in text and "all done" in text
    folded = env["inbox"].drain()
    assert len(folded) == 1 and "job #1" in folded[0] and "all done" in folded[0]


def test_run_job_failure_path(tmp_path):
    env = runner_env(tmp_path, result=ClaudeResult(text="", session_id=None, is_error=True, error="boom"))
    assert run_with(env) == 1
    job = env["store"].get(1)
    assert job["state"] == "failed"
    assert job["error"] == "boom"
    assert "failed" in env["pings"][0][1]
    assert any("failed" in note for note in env["inbox"].drain())


def test_run_job_refuses_a_non_pending_job(tmp_path):
    env = runner_env(tmp_path, result=ClaudeResult(text="x", session_id=None, is_error=False))
    env["store"].transition(1, ("pending",), "running")
    assert run_with(env) == 1
    assert env["driver"].prompts == []  # no model call for a job we did not own


def test_run_job_resolves_the_workspace(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    env = runner_env(
        tmp_path, workspace=ws,
        result=ClaudeResult(text="ok", session_id=None, is_error=False),
    )
    assert run_with(env) == 0
    assert env["captured"]["workspace_path"] == str(ws.resolve())


def test_run_job_fails_cleanly_on_unknown_workspace(tmp_path):
    env = runner_env(tmp_path, result=ClaudeResult(text="ok", session_id=None, is_error=False))
    env["store"].update(1, workspace="gone")
    assert run_with(env) == 1
    job = env["store"].get(1)
    assert job["state"] == "failed"
    assert "workspace" in job["error"]
    assert env["driver"].prompts == []  # never launched claude


def test_run_job_delivers_artifacts(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / "out.md").write_text("report", encoding="utf-8")
    env = runner_env(
        tmp_path, workspace=ws,
        result=ClaudeResult(text="done\nARTIFACT: out.md\nARTIFACT: missing.bin",
                            session_id=None, is_error=False),
    )
    assert run_with(env) == 0
    job = env["store"].get(1)
    assert job["artifacts"] == ["out.md"]
    assert env["files"] == [("chan-9", str((ws / "out.md").resolve()))]
    # the unresolvable artifact is named in the fold-back, never silent
    folded = env["inbox"].drain()
    assert any("missing.bin" in note for note in folded)


def test_default_driver_factory_uses_derived_denylist(tmp_path):
    from iris.jobs import build_job_driver

    config = Config(jobs_enabled=True, claude_bin="claude", model="m-strong",
                    turn_timeout=300.0, job_timeout=1234.0)
    job = {"grants": ["subagents", "files"], "workspace": ""}
    driver = build_job_driver(config, job, None)
    assert driver.disallowed_tools == job_disallowed(["subagents", "files"])
    assert driver.allowed_tools == job_allowed_builtins(["subagents", "files"])
    assert driver.timeout == 1234.0
    assert driver.model == "m-strong"
    assert driver.add_dirs is None


def test_default_driver_factory_adds_workspace_dir():
    from iris.jobs import build_job_driver

    config = Config(jobs_enabled=True, job_model="m-light")
    driver = build_job_driver(config, {"grants": ["subagents"], "workspace": "ws"}, "/some/dir")
    assert driver.add_dirs == ["/some/dir"]
    assert driver.model == "m-light"  # IRIS_JOB_MODEL overrides the chat model


# -- config knobs --------------------------------------------------------------


def test_jobs_config_knobs(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IRIS_JOBS", "true")
    monkeypatch.setenv("IRIS_JOBS_FILE", "j.json")
    monkeypatch.setenv("IRIS_JOB_GRANTS", "shell, files")
    monkeypatch.setenv("IRIS_JOBS_MAX", "5")
    monkeypatch.setenv("IRIS_JOB_TIMEOUT", "900")
    monkeypatch.setenv("IRIS_JOB_MODEL", "claude-haiku-4-5-20251001")
    monkeypatch.setenv("IRIS_INBOX_FILE", "inbox.json")
    monkeypatch.setenv("IRIS_DISCORD_HOME_CHANNEL", "123")
    cfg = Config.from_env(dotenv=tmp_path / "none.env")
    assert cfg.jobs_enabled is True
    assert cfg.jobs_file == "j.json"
    assert cfg.job_grants == ["shell", "files"]
    assert cfg.jobs_max == 5
    assert cfg.job_timeout == 900.0
    assert cfg.job_model == "claude-haiku-4-5-20251001"
    assert cfg.inbox_file == "inbox.json"
    assert cfg.home_channel == "123"


def test_jobs_config_defaults(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    cfg = Config.from_env(dotenv=tmp_path / "none.env")
    assert cfg.jobs_enabled is False
    assert cfg.jobs_max == 2
    assert cfg.job_timeout == 1800.0
    assert cfg.job_grants == []


def test_agent_from_config_builds_inbox_only_when_jobs_are_on(tmp_path):
    from iris.agent import Agent

    base = dict(session_store_path=str(tmp_path / "s.json"),
                inbox_file=str(tmp_path / "i.json"))
    assert Agent.from_config(Config(jobs_enabled=False, **base)).inbox is None
    agent = Agent.from_config(Config(jobs_enabled=True, **base))
    assert agent.inbox is not None


def test_cli_job_run_gate_and_dispatch(tmp_path, monkeypatch):
    from iris.cli import main
    import iris.jobs as jobs_mod

    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    assert main(["job-run", "7"]) == 1  # jobs are off by default

    monkeypatch.setenv("IRIS_JOBS", "true")
    seen = {}

    def fake_run_job(job_id, config, **kwargs):
        seen["id"] = job_id
        return 0

    monkeypatch.setattr(jobs_mod, "run_job", fake_run_job)
    assert main(["job-run", "7"]) == 0
    assert seen["id"] == 7
