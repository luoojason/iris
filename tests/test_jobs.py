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

    def driver_factory(config, job, workspace_path, child_pid_callback=None):
        captured["workspace_path"] = workspace_path
        captured["job"] = dict(job)
        return driver

    pings = []
    files = []
    config = Config(jobs_enabled=True, discord_token="tok",
                    usage_file=str(tmp_path / "usage.json"))
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


def test_agent_from_config_always_builds_the_inbox(tmp_path):
    """Wakes queue fold-back notes whether or not jobs are enabled, so the
    inbox must drain regardless of IRIS_JOBS."""
    from iris.agent import Agent

    base = dict(session_store_path=str(tmp_path / "s.json"),
                inbox_file=str(tmp_path / "i.json"))
    assert Agent.from_config(Config(jobs_enabled=False, **base)).inbox is not None
    assert Agent.from_config(Config(jobs_enabled=True, **base)).inbox is not None


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


def test_full_grants_disable_the_denylist_instead_of_reviving_it():
    """All grants -> empty derived denylist. The driver treats a falsy explicit
    denylist as unset and falls back to the FULL default, silently re-denying
    every granted tool; the job driver must disable that fallback."""
    from iris.jobs import build_job_driver

    config = Config(jobs_enabled=True)
    driver = build_job_driver(
        config, {"grants": ["subagents", "shell", "files"], "workspace": ""}, None)
    assert driver.disallowed_tools == ()
    assert "--disallowedTools" not in driver.build_command()
    assert "Bash" in (driver.allowed_tools or [])


def test_job_driver_runs_in_the_workspace(tmp_path):
    from iris.jobs import build_job_driver

    config = Config(jobs_enabled=True)
    driver = build_job_driver(config, {"grants": ["subagents"], "workspace": "ws"},
                              str(tmp_path))
    assert driver.cwd == str(tmp_path)


def test_job_driver_without_workspace_avoids_the_secrets_dir(tmp_path, monkeypatch):
    """The agent's own dir holds .env and state files; a job's claude child
    must never have it as cwd (the Read tool is always available)."""
    import tempfile

    from iris.jobs import build_job_driver

    monkeypatch.chdir(tmp_path)  # pretend tmp_path is the iris dir
    config = Config(jobs_enabled=True)
    driver = build_job_driver(config, {"grants": ["subagents"], "workspace": ""}, None)
    assert driver.cwd is not None
    assert driver.cwd != str(tmp_path)
    assert driver.cwd.startswith(tempfile.gettempdir())


def test_run_job_records_the_claude_child_pid(tmp_path):
    env = runner_env(tmp_path, result=ClaudeResult(text="ok", session_id=None, is_error=False))

    def factory_with_callback(config, job, workspace_path, child_pid_callback=None):
        class D:
            def run(self, prompt, session_id=None, model=None):
                child_pid_callback(7777)  # what ClaudeDriver does on spawn
                return ClaudeResult(text="ok", session_id=None, is_error=False)
        return D()

    env["driver_factory"] = factory_with_callback
    assert run_with(env) == 0
    assert env["store"].get(1)["claude_pid"] == 7777


def test_run_job_survives_a_raising_driver(tmp_path):
    from iris.driver import ClaudeError

    env = runner_env(tmp_path, result=None)

    def exploding_factory(config, job, workspace_path, child_pid_callback=None):
        class D:
            def run(self, prompt, session_id=None, model=None):
                raise ClaudeError("claude binary not found on PATH")
        return D()

    env["driver_factory"] = exploding_factory
    assert run_with(env) == 1
    job = env["store"].get(1)
    assert job["state"] == "failed"
    assert "crashed" in job["error"]
    assert len(env["pings"]) == 1 and "failed" in env["pings"][0][1]
    assert env["inbox"].drain()  # the owner is never left guessing


def test_run_job_keeps_the_paid_report_when_artifact_handling_crashes(tmp_path, monkeypatch):
    import iris.jobs as jobs_mod

    ws = tmp_path / "repo"
    ws.mkdir()
    env = runner_env(tmp_path, workspace=ws,
                     result=ClaudeResult(text="precious report", session_id=None, is_error=False))

    def explode(report, workspace_dir):
        raise RuntimeError("artifact bug")

    monkeypatch.setattr(jobs_mod, "collect_artifacts", explode)
    assert run_with(env) == 1
    job = env["store"].get(1)
    assert job["state"] == "failed"
    assert job["report"] == "precious report"  # the billed turn's output survives
    assert "artifact" in job["error"]
    assert len(env["pings"]) == 1


def test_run_job_skips_delivery_when_cancelled_mid_run(tmp_path):
    env = runner_env(tmp_path, result=None)
    store = env["store"]

    def cancelling_factory(config, job, workspace_path, child_pid_callback=None):
        class D:
            def run(self, prompt, session_id=None, model=None):
                store.transition(1, ("running",), "cancelled")  # owner cancelled mid-turn
                return ClaudeResult(text="too late", session_id=None, is_error=False)
        return D()

    env["driver_factory"] = cancelling_factory
    assert run_with(env) == 0
    assert store.get(1)["state"] == "cancelled"
    assert env["pings"] == []  # no confusing 'finished' after a cancel
    assert env["inbox"].drain() == []


def test_artifact_problems_survive_a_long_report(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    long_report = ("x" * 3000) + "\nARTIFACT: missing.bin"
    env = runner_env(tmp_path, workspace=ws,
                     result=ClaudeResult(text=long_report, session_id=None, is_error=False))
    assert run_with(env) == 0
    text = env["pings"][0][1]
    assert "missing.bin" in text  # the skip note outlives report truncation
    assert "truncated" in text


def test_ping_success_marks_report_delivered(tmp_path):
    env = runner_env(tmp_path, result=ClaudeResult(text="ok", session_id=None, is_error=False))
    run_with(env)
    assert env["store"].get(1)["report_delivered"] is True


def test_failed_ping_leaves_report_undelivered(tmp_path):
    env = runner_env(tmp_path, result=ClaudeResult(text="ok", session_id=None, is_error=False))
    env["send_message"] = lambda channel, text, token: False
    run_with(env)
    assert env["store"].get(1)["report_delivered"] is False


def test_repair_flips_spawned_but_dead_pending_jobs(tmp_path, monkeypatch):
    import iris.jobs as jobs_mod
    from iris.jobs import repair_dead_runners

    store = make_store(tmp_path)
    store.add("spawned", "x", [], "", "")
    store.update(1, pid=4242)  # the spawn recorded a runner pid
    store.add("queued", "y", [], "", "")  # never spawned: pid stays None
    monkeypatch.setattr(jobs_mod, "_pid_alive", lambda pid: False)
    assert repair_dead_runners(store) == 1
    assert store.get(1)["state"] == "failed"
    assert store.get(2)["state"] == "pending"  # genuinely queued jobs are untouched


def test_spawn_runner_records_the_pid(tmp_path):
    from iris.jobs import spawn_runner

    store = make_store(tmp_path)
    store.add("a", "x", [], "", "")

    class FakeProc:
        pid = 31337

    spawn_runner(1, store=store, popen=lambda *a, **k: FakeProc())
    assert store.get(1)["pid"] == 31337


def test_store_admission_is_atomic_with_add(tmp_path):
    store = make_store(tmp_path)
    store.add("a", "x", [], "", "")
    store.add("b", "y", [], "", "")  # two active
    job = store.add("c", "z", [], "", "", admit_below=2)
    assert job["admitted"] is False
    store.transition(1, ("pending",), "done")
    store.transition(3, ("pending",), "cancelled")  # queued jobs count as active
    job = store.add("d", "w", [], "", "", admit_below=2)
    assert job["admitted"] is True
    # the ephemeral flag never lands in the file
    assert "admitted" not in store.get(job["id"])


def test_send_discord_file_sanitizes_the_header_filename(tmp_path, monkeypatch):
    import iris.jobs as jobs_mod
    from iris.jobs import send_discord_file

    hostile = tmp_path / 'a"b\rc.txt'
    hostile.write_bytes(b"data")
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=None):
        captured["body"] = req.data
        return FakeResponse()

    monkeypatch.setattr(jobs_mod.urllib.request, "urlopen", fake_urlopen)
    res = send_discord_file("chan", str(hostile), "job #1 artifact", "tok")
    assert res.get("ok")
    body = captured["body"]
    # the multipart headers carry no quote or CR from the hostile name
    header_zone = body.split(b"\r\n\r\ndata", 1)[0]
    assert b'a"b' not in header_zone
    assert b"b\rc" not in header_zone
    # but the real filename still reaches Discord inside the JSON payload
    assert b'a\\"b\\rc.txt' in body


def test_corrupt_store_is_quarantined_not_silently_dropped(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text('[{"id": 1, "title": "precious"}]\nGARBAGE', encoding="utf-8")
    store = JobStore(path)
    assert store.all() == []  # recovered to fresh
    sidecar = path.with_suffix(".json.corrupt")
    assert sidecar.exists()  # the owner's data is preserved for recovery
    assert "precious" in sidecar.read_text("utf-8")


def test_artifact_upload_failure_is_reported_not_silent(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / "out.md").write_text("report", encoding="utf-8")
    env = runner_env(tmp_path, workspace=ws,
                     result=ClaudeResult(text="done\nARTIFACT: out.md",
                                         session_id=None, is_error=False))
    env["send_file"] = lambda channel, path, text, token: {"error": "HTTP 413"}
    assert run_with(env) == 0  # the job still completes
    folded = env["inbox"].drain()
    assert any("failed to upload" in note and "HTTP 413" in note for note in folded)


# -- auto-prune + manual prune + rerun (job console foundation) --------------


def test_auto_prune_keeps_recent_terminal_jobs(tmp_path):
    store = JobStore(tmp_path / "jobs.json", keep=2)
    ids = [store.add(f"t{n}", "x", [], "", "")["id"] for n in range(5)]
    for i in ids:  # all terminal
        store.transition(i, ("pending",), "done", finished_ts=float(i))
    # adding one more prunes terminal jobs down to keep=2 (most recent kept)
    store.add("fresh", "x", [], "", "")
    kept = {j["id"] for j in store.all()}
    # the 2 most-recent terminal (ids 4,5) + the new pending job survive
    assert ids[4] in kept and ids[3] in kept
    assert ids[0] not in kept and ids[1] not in kept and ids[2] not in kept


def test_auto_prune_never_drops_active_jobs(tmp_path):
    store = JobStore(tmp_path / "jobs.json", keep=1)
    a = store.add("active1", "x", [], "", "")["id"]
    b = store.add("active2", "x", [], "", "")["id"]
    store.transition(b, ("pending",), "running")
    # a stays pending, b running; both active. add more terminal churn:
    for n in range(4):
        jid = store.add(f"term{n}", "x", [], "", "")["id"]
        store.transition(jid, ("pending",), "failed")
    states = {j["id"]: j["state"] for j in store.all()}
    assert states[a] == "pending" and states[b] == "running"  # never pruned
    terminal = [j for j in store.all() if j["state"] in ("done", "failed", "cancelled")]
    assert len(terminal) == 1  # keep=1


def test_manual_prune_returns_count_and_keeps_most_recent(tmp_path):
    store = JobStore(tmp_path / "jobs.json")  # no auto-prune
    ids = [store.add(f"t{n}", "x", [], "", "")["id"] for n in range(6)]
    for i in ids:
        store.transition(i, ("pending",), "cancelled")
    dropped = store.prune(keep=2)
    assert dropped == 4
    remaining = sorted(j["id"] for j in store.all())
    assert remaining == ids[-2:]  # the 2 highest ids


def test_prune_no_op_when_under_cap(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("a", "x", [], "", "")
    assert store.prune(keep=50) == 0


def test_jobs_keep_config_knob(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IRIS_JOBS_KEEP", "10")
    assert Config.from_env(dotenv=tmp_path / "none.env").jobs_keep == 10
    monkeypatch.delenv("IRIS_JOBS_KEEP")
    assert Config.from_env(dotenv=tmp_path / "none.env").jobs_keep == 50




def test_prune_keep_zero_preserves_the_id_anchor(tmp_path):
    """keep=0 must not empty the registry of its highest id, or the next add()
    would reuse an id already delivered to the owner."""
    store = JobStore(tmp_path / "jobs.json")
    store.add("a", "x", [], "", "")
    store.add("b", "x", [], "", "")
    store.transition(1, ("pending",), "done")
    store.transition(2, ("pending",), "done")
    store.prune(keep=0)
    # the highest-id terminal job is kept as the monotonic anchor
    assert store.add("c", "x", [], "", "")["id"] == 3  # never reuses #1/#2


def test_auto_prune_keep_zero_never_reuses_ids(tmp_path):
    store = JobStore(tmp_path / "jobs.json", keep=0)
    j1 = store.add("a", "x", [], "", "")["id"]
    store.transition(j1, ("pending",), "done")  # auto-prune fires, keep=0
    j2 = store.add("b", "x", [], "", "")["id"]
    assert j2 > j1  # monotonic despite keep=0


# -- browser grant --------------------------------------------------------------


def test_browser_is_a_known_grant_with_no_builtin_unlocks():
    from iris.jobs import job_allowed_builtins, parse_grants

    assert parse_grants("browser") == ["browser"]
    # it unlocks no dangerous built-ins; the capability arrives via MCP
    assert job_allowed_builtins(["browser"]) == []
    assert job_disallowed(["browser"]) == job_disallowed([])


def test_browser_grant_is_clamped_by_the_ceiling():
    from iris.jobs import clamp_grants

    granted, clamped = clamp_grants(["browser"], [])
    assert "browser" not in granted and clamped == ["browser"]
    granted, clamped = clamp_grants(["browser"], ["browser"])
    assert "browser" in granted and clamped == []


def test_browser_grant_wires_the_playwright_mcp(tmp_path):
    import json as _json

    from iris.jobs import build_job_driver

    config = Config(jobs_enabled=True, job_grants=["browser"],
                    browser_profile_dir=str(tmp_path / "profile"))
    job = {"grants": ["subagents", "browser"], "workspace": ""}
    driver = build_job_driver(config, job, None)
    assert driver.mcp_config is not None
    spec = _json.loads(open(driver.mcp_config, encoding="utf-8").read())
    server = spec["mcpServers"]["playwright"]
    assert "--user-data-dir" in server["args"]
    profile = server["args"][server["args"].index("--user-data-dir") + 1]
    assert profile == str((tmp_path / "profile").resolve())
    assert "mcp__playwright" in driver.allowed_tools


def test_no_browser_grant_means_no_mcp_config(tmp_path):
    from iris.jobs import build_job_driver

    config = Config(jobs_enabled=True, job_grants=["browser"])
    driver = build_job_driver(config, {"grants": ["subagents"], "workspace": ""}, None)
    assert driver.mcp_config is None
    assert not any("playwright" in t for t in (driver.allowed_tools or []))


def test_browser_mcp_command_is_owner_configurable(tmp_path):
    import json as _json

    from iris.jobs import build_job_driver

    config = Config(jobs_enabled=True, job_grants=["browser"],
                    browser_mcp_cmd="node /opt/playwright-mcp/cli.js --headless",
                    browser_profile_dir=str(tmp_path / "p"))
    driver = build_job_driver(config, {"grants": ["browser"], "workspace": ""}, None)
    spec = _json.loads(open(driver.mcp_config, encoding="utf-8").read())
    server = spec["mcpServers"]["playwright"]
    assert server["command"] == "node"
    assert "/opt/playwright-mcp/cli.js" in server["args"]


def test_browser_config_knobs(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IRIS_BROWSER_MCP_CMD", "npx custom-mcp")
    monkeypatch.setenv("IRIS_BROWSER_PROFILE_DIR", "/tmp/prof")
    cfg = Config.from_env(dotenv=tmp_path / "none.env")
    assert cfg.browser_mcp_cmd == "npx custom-mcp"
    assert cfg.browser_profile_dir == "/tmp/prof"


def test_browser_grant_denies_only_the_code_execution_tools_by_default(tmp_path):
    from iris.jobs import build_job_driver

    config = Config(jobs_enabled=True, job_grants=["browser"],
                    browser_profile_dir=str(tmp_path / "p"))
    driver = build_job_driver(config, {"grants": ["browser"], "workspace": ""}, None)
    denied = list(driver.disallowed_tools)
    # in-page code execution stays denied: a human does not need it to use a site
    assert "mcp__playwright__browser_evaluate" in denied
    assert "mcp__playwright__browser_run_code_unsafe" in denied
    # uploads are allowed by default now (a human uploads profile pics, docs)
    assert "mcp__playwright__browser_file_upload" not in denied
    # the derived built-in denylist is still intact underneath
    for tool in job_disallowed(["browser"]):
        assert tool in denied


def test_browser_deny_list_is_configurable(tmp_path):
    from iris.jobs import build_job_driver

    config = Config(jobs_enabled=True, job_grants=["browser"],
                    browser_deny_tools=["browser_file_upload"],
                    browser_profile_dir=str(tmp_path / "p"))
    driver = build_job_driver(config, {"grants": ["browser"], "workspace": ""}, None)
    denied = list(driver.disallowed_tools)
    assert "mcp__playwright__browser_file_upload" in denied
    assert "mcp__playwright__browser_evaluate" not in denied


def test_browser_deny_list_empty_denies_no_mcp_tools(tmp_path):
    from iris.jobs import build_job_driver

    config = Config(jobs_enabled=True, job_grants=["browser"],
                    browser_deny_tools=[], browser_profile_dir=str(tmp_path / "p"))
    driver = build_job_driver(config, {"grants": ["browser"], "workspace": ""}, None)
    assert not any("playwright" in t for t in driver.disallowed_tools)
    # the built-in denylist is untouched by the browser knob
    for tool in job_disallowed(["browser"]):
        assert tool in driver.disallowed_tools
