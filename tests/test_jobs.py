"""Tests for the hybrid job coordinator (iris/jobs.py)."""

from __future__ import annotations

import json
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
        self.calls = []  # (prompt, session_id) so resume can be asserted

    def run(self, prompt, session_id=None, model=None, conversation_id=None):
        self.prompts.append(prompt)
        self.calls.append((prompt, session_id))
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
        verify=env.get("verify"),
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
    folded = env["inbox"].drain("discord:chan-9")
    assert len(folded) == 1 and "job #1" in folded[0] and "all done" in folded[0]


def test_run_job_verification_pass_delivers_clean(tmp_path):
    env = runner_env(tmp_path, result=ClaudeResult(text="wrote report.md", session_id="s", is_error=False))
    env["verify"] = lambda instructions, report: {"ok": True, "reason": "file present"}
    assert run_with(env) == 0
    assert env["store"].get(1)["verified"] is True
    text = env["pings"][0][1]
    assert "verification flag" not in text  # a pass adds no warning
    assert "wrote report.md" in text


def test_run_job_verification_fail_flags_but_still_delivers(tmp_path):
    env = runner_env(tmp_path, result=ClaudeResult(text="I think it's basically done", session_id="s", is_error=False))
    env["verify"] = lambda instructions, report: {"ok": False, "reason": "no file was produced"}
    assert run_with(env) == 0
    job = env["store"].get(1)
    assert job["verified"] is False
    assert job["state"] == "done"  # the work still completed; verification only annotates
    text = env["pings"][0][1]
    assert "verification flag" in text and "no file was produced" in text
    assert "I think it's basically done" in text  # the full report is still delivered
    assert job["verify_reason"] == "no file was produced"  # reason persisted for later
    # the fold-back note carries the warning too
    assert any("verification flag" in n for n in env["inbox"].drain("discord:chan-9"))


def test_extract_question_finds_the_marker():
    from iris.jobs import extract_question

    assert extract_question("did some setup\nQUESTION: prod or staging?") == "prod or staging?"
    assert extract_question("QUESTION: which DB?\nthe two options are A and B") == \
        "which DB?\nthe two options are A and B"
    assert extract_question("all finished, no question here") is None
    assert extract_question("") is None


def test_run_job_pauses_to_ask_when_the_report_has_a_question(tmp_path):
    env = runner_env(tmp_path, result=ClaudeResult(
        text="I set things up.\nQUESTION: deploy to prod or staging?",
        session_id="sess-1", is_error=False))
    assert run_with(env) == 0
    job = env["store"].get(1)
    assert job["state"] == "needs_input"
    assert "prod or staging" in job["question"]
    assert job["session_id"] == "sess-1"  # stored so the answer can resume it
    assert job["question_rounds"] == 1
    assert job.get("finished_ts") is None  # it is paused, not finished
    ping = env["pings"][0][1]
    assert "needs your input" in ping.lower() and "prod or staging" in ping
    assert "finished" not in ping.lower()


def test_run_job_resumes_the_session_with_the_owner_answer(tmp_path):
    env = runner_env(tmp_path, result=ClaudeResult(
        text="done, used staging", session_id="sess-2", is_error=False))
    # the job is waiting; resume_job recorded the answer and set it pending again
    env["store"].update(1, pending_answer="use staging", session_id="sess-1", question_rounds=1)
    assert run_with(env) == 0
    job = env["store"].get(1)
    assert job["state"] == "done"
    assert env["driver"].calls[0] == ("use staging", "sess-1")  # resumed, not re-instructed
    assert not job.get("pending_answer")  # consumed


def test_run_job_stops_asking_after_the_question_round_cap(tmp_path):
    env = runner_env(tmp_path, result=ClaudeResult(
        text="QUESTION: yet another fork?", session_id="s", is_error=False))
    env["config"].job_max_questions = 1
    env["store"].update(1, question_rounds=1)  # already at the cap
    assert run_with(env) == 0
    job = env["store"].get(1)
    assert job["state"] == "done"  # no more pausing
    assert "yet another fork?" in job["report"]  # the question rides along in the report


def test_cancel_handles_a_waiting_job(tmp_path):
    from iris.jobs import cancel

    store = make_store(tmp_path)
    store.add("j", "i", ["subagents"], "", "h")
    store.transition(1, ("pending",), "needs_input", question="q?")
    out = cancel(store, 1)
    assert "ancel" in out and store.get(1)["state"] == "cancelled"


# -- active-jobs digest (tier-0 awareness so a turn can't duplicate work) -----

def test_jobs_digest_lists_active_and_recently_finished():
    from iris.jobs import jobs_digest
    now = 1000.0
    jobs = [
        {"id": 27, "state": "running", "title": "Publish 5 parked Top-5 Shorts", "finished_ts": None},
        {"id": 10, "state": "done", "title": "recent done", "finished_ts": now - 100},
        {"id": 9, "state": "done", "title": "ancient done", "finished_ts": now - 99999},
        {"id": 5, "state": "pending", "title": "queued one", "finished_ts": None},
    ]
    out = jobs_digest(jobs, now, max_bytes=600, recent_secs=3600)
    assert "#27 [running] Publish 5 parked Top-5 Shorts" in out
    assert "#5 [pending]" in out
    assert "#10 [done] recent done" in out      # just-finished still visible
    assert "ancient done" not in out            # old terminal omitted
    assert "duplicate" in out.lower()           # the steering header is present


def test_jobs_digest_empty_when_nothing_active_or_recent():
    from iris.jobs import jobs_digest
    assert jobs_digest([], 1000.0) == ""
    # a long-finished terminal job alone -> nothing to show
    assert jobs_digest([{"id": 1, "state": "done", "title": "x", "finished_ts": 0.0}], 1e9) == ""


def test_jobs_digest_byte_budget_skips_long_lines():
    from iris.jobs import jobs_digest
    now = 1000.0
    jobs = [{"id": 1, "state": "running", "title": "x" * 2000, "finished_ts": None},
            {"id": 2, "state": "running", "title": "short one", "finished_ts": None}]
    out = jobs_digest(jobs, now, max_bytes=220)
    assert "short one" in out and "xxxx" not in out  # long line skipped, short kept
    assert len(out.encode("utf-8")) <= 220


def test_find_duplicate_job_matches_active_same_title_and_channel():
    from iris.jobs import JobStore, find_duplicate_job

    import os as _os
    store = JobStore("/tmp/iris-dedup-test-%d.json" % _os.getpid())
    store.add("Upload the 5 parked shorts", "i", ["subagents"], "", "chan-9", state="running")
    store.add("totally different job", "i", ["subagents"], "", "chan-9", state="running")
    # near-identical title on the same channel -> flagged
    dup = find_duplicate_job(store, "upload the 5 PARKED shorts!", "chan-9")
    assert dup is not None and dup["id"] == 1
    # different channel -> not a duplicate
    assert find_duplicate_job(store, "Upload the 5 parked shorts", "other-ch") is None
    # unrelated title -> not a duplicate
    assert find_duplicate_job(store, "build a new short", "chan-9") is None
    _os.unlink(store.path)


# -- silent job-death notification ------------------------------------------

def _death_cfg(tmp_path, **kw):
    base = dict(jobs_enabled=True, discord_token="tok", home_channel="home-1",
                jobs_file=str(tmp_path / "jobs.json"),
                inbox_file=str(tmp_path / "inbox.json"))
    base.update(kw)
    return Config(**base)


def test_notify_dead_jobs_pings_once_for_a_runner_that_died(tmp_path):
    from iris.jobs import notify_dead_jobs

    cfg = _death_cfg(tmp_path)
    store = JobStore(cfg.jobs_file)
    store.add("browser pull", "i", ["subagents"], "", "chan-9", state="running")
    store.update(1, pid=999999)  # a pid that is not alive: the runner died

    sent = []
    n = notify_dead_jobs(cfg, send=lambda c, t, k: sent.append((c, t)) or True)
    assert n == 1
    assert store.get(1)["state"] == "failed"
    assert sent and sent[0][0] == "chan-9" and "failed" in sent[0][1].lower()
    assert any("failed" in note.lower() for note in Inbox(cfg.inbox_file).drain("discord:chan-9"))

    # a second sweep does not re-ping the same death
    sent.clear()
    assert notify_dead_jobs(cfg, send=lambda c, t, k: sent.append(1) or True) == 0
    assert sent == []


def test_notify_dead_jobs_does_not_reflood_on_a_permanently_failing_send(tmp_path):
    # Regression: a deleted/forbidden channel (send always False) must not re-fold
    # the inbox note every tick. The fold is the durable record; the ping is
    # best-effort, so the death is claimed once regardless of send success.
    from iris.jobs import notify_dead_jobs

    cfg = _death_cfg(tmp_path)
    store = JobStore(cfg.jobs_file)
    store.add("pull", "i", ["subagents"], "", "chan-dead", state="running")
    store.update(1, pid=999999)  # the runner died
    for _ in range(3):
        notify_dead_jobs(cfg, send=lambda c, t, k: False)  # Discord permanently down
    notes = Inbox(cfg.inbox_file).drain("discord:chan-dead")
    assert len(notes) == 1  # exactly one note despite three sweeps


def test_notify_dead_jobs_leaves_clean_failures_and_healthy_jobs_alone(tmp_path):
    import os

    from iris.jobs import notify_dead_jobs

    cfg = _death_cfg(tmp_path)
    store = JobStore(cfg.jobs_file)
    # a cleanly-failed job already delivered its own failure ping
    store.add("clean fail", "i", ["subagents"], "", "h")
    store.transition(1, ("pending",), "failed", error="boom")
    # a healthy running job with a live pid
    store.add("healthy", "i", ["subagents"], "", "h", state="running")
    store.update(2, pid=os.getpid())

    sent = []
    n = notify_dead_jobs(cfg, send=lambda c, t, k: sent.append(1) or True)
    assert n == 0 and sent == []
    assert store.get(2)["state"] == "running"  # healthy job untouched


class _ParkedGuard:
    def should_park(self):
        return True

    def record(self, *a, **k):
        pass


class _OpenGuard:
    def should_park(self):
        return False

    def record(self, *a, **k):
        pass


# -- after:<job_id> chaining -------------------------------------------------

def test_launch_ready_dependents_launches_when_prereq_is_done(tmp_path):
    from iris.jobs import launch_ready_dependents

    store = make_store(tmp_path)
    a = store.add("A", "i", ["subagents"], "", "chan", state="done")
    b = store.add("B", "i", ["subagents"], "", "chan", state="waiting", after=a["id"])
    spawned = []
    n = launch_ready_dependents(store, Config(), spawn=lambda jid, **k: spawned.append(jid),
                                guard=_OpenGuard(), notify=lambda dep, m: None)
    assert n == 1 and spawned == [b["id"]]
    assert store.get(b["id"])["state"] == "pending"


def test_launch_ready_dependents_cancels_when_prereq_failed(tmp_path):
    from iris.jobs import launch_ready_dependents

    store = make_store(tmp_path)
    a = store.add("A", "i", ["subagents"], "", "chan", state="failed")
    b = store.add("B", "i", ["subagents"], "", "chan", state="waiting", after=a["id"])
    spawned = []
    launch_ready_dependents(store, Config(), spawn=lambda jid, **k: spawned.append(jid),
                            guard=_OpenGuard(), notify=lambda dep, m: None)
    assert spawned == []
    assert store.get(b["id"])["state"] == "cancelled"


def test_launch_ready_dependents_waits_while_prereq_active(tmp_path):
    from iris.jobs import launch_ready_dependents

    store = make_store(tmp_path)
    a = store.add("A", "i", ["subagents"], "", "chan", state="running")
    b = store.add("B", "i", ["subagents"], "", "chan", state="waiting", after=a["id"])
    spawned = []
    n = launch_ready_dependents(store, Config(), spawn=lambda jid, **k: spawned.append(jid),
                                guard=_OpenGuard(), notify=lambda dep, m: None)
    assert n == 0 and spawned == []
    assert store.get(b["id"])["state"] == "waiting"


def test_launch_ready_dependents_parks_when_the_guard_is_parked(tmp_path):
    from iris.jobs import launch_ready_dependents

    store = make_store(tmp_path)
    a = store.add("A", "i", ["subagents"], "", "chan", state="done")
    b = store.add("B", "i", ["subagents"], "", "chan", state="waiting", after=a["id"])
    spawned = []
    launch_ready_dependents(store, Config(), spawn=lambda jid, **k: spawned.append(jid),
                            guard=_ParkedGuard(), notify=lambda dep, m: None)
    assert spawned == []
    assert store.get(b["id"])["state"] == "parked"


def test_run_job_launches_a_chained_dependent_on_success(tmp_path):
    env = runner_env(tmp_path, result=ClaudeResult(text="done", session_id="s", is_error=False))
    dep = env["store"].add("B", "i", ["subagents"], "", "chan-9", state="waiting",
                           after=env["job_id"])
    spawned = []
    run_job(env["job_id"], env["config"], store=env["store"], workspace_store=env["ws_store"],
            inbox=env["inbox"], driver_factory=env["driver_factory"],
            send_message=env["send_message"], send_file=env["send_file"],
            spawn=lambda jid, **k: spawned.append(jid))
    assert env["store"].get(env["job_id"])["state"] == "done"
    assert spawned == [dep["id"]]  # the chained job launched when its prereq finished
    assert env["store"].get(dep["id"])["state"] == "pending"


def test_run_job_skips_verification_when_the_credit_guard_is_parked(tmp_path):
    env = runner_env(tmp_path, result=ClaudeResult(text="done", session_id="s", is_error=False))
    calls = []
    run_job(env["job_id"], env["config"], store=env["store"], workspace_store=env["ws_store"],
            inbox=env["inbox"], driver_factory=env["driver_factory"],
            send_message=env["send_message"], send_file=env["send_file"],
            verify=lambda i, r: calls.append((i, r)) or {"ok": False, "reason": "x"},
            guard=_ParkedGuard())
    assert calls == []  # parked: no extra reviewer spend
    assert env["store"].get(1)["verified"] is None


def test_run_job_verification_fails_open_when_the_reviewer_crashes(tmp_path):
    env = runner_env(tmp_path, result=ClaudeResult(text="done", session_id="s", is_error=False))

    def boom(instructions, report):
        raise RuntimeError("reviewer model unreachable")

    env["verify"] = boom
    assert run_with(env) == 0  # the report is delivered regardless
    job = env["store"].get(1)
    assert job["verified"] is None  # unverified, not failed
    assert "verification flag" not in env["pings"][0][1]  # no false alarm


def test_run_job_without_verification_is_unchanged(tmp_path):
    # No verify seam (the default): no verified field is forced true/false, and
    # the report delivers exactly as before.
    env = runner_env(tmp_path, result=ClaudeResult(text="all done", session_id="s", is_error=False))
    assert run_with(env) == 0
    assert env["store"].get(1)["verified"] is None
    assert "verification flag" not in env["pings"][0][1]


def test_run_job_failure_path(tmp_path):
    env = runner_env(tmp_path, result=ClaudeResult(text="", session_id=None, is_error=True, error="boom"))
    assert run_with(env) == 1
    job = env["store"].get(1)
    assert job["state"] == "failed"
    assert job["error"] == "boom"
    assert "failed" in env["pings"][0][1]
    assert any("failed" in note for note in env["inbox"].drain("discord:chan-9"))


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
    folded = env["inbox"].drain("discord:chan-9")
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


def test_heavy_job_escalates_to_the_strong_model():
    from iris.jobs import build_job_driver

    config = Config(jobs_enabled=True, job_model="m-light", job_model_heavy="m-strong")
    light = build_job_driver(config, {"grants": ["subagents"], "heavy": False}, None)
    heavy = build_job_driver(config, {"grants": ["subagents"], "heavy": True}, None)
    assert light.model == "m-light"     # everyday job stays on the cheap model
    assert heavy.model == "m-strong"    # hard job escalates


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
            def run(self, prompt, session_id=None, model=None, conversation_id=None):
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
            def run(self, prompt, session_id=None, model=None, conversation_id=None):
                raise ClaudeError("claude binary not found on PATH")
        return D()

    env["driver_factory"] = exploding_factory
    assert run_with(env) == 1
    job = env["store"].get(1)
    assert job["state"] == "failed"
    assert "crashed" in job["error"]
    assert len(env["pings"]) == 1 and "failed" in env["pings"][0][1]
    assert env["inbox"].drain("discord:chan-9")  # the owner is never left guessing


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
            def run(self, prompt, session_id=None, model=None, conversation_id=None):
                store.transition(1, ("running",), "cancelled")  # owner cancelled mid-turn
                return ClaudeResult(text="too late", session_id=None, is_error=False)
        return D()

    env["driver_factory"] = cancelling_factory
    assert run_with(env) == 0
    assert store.get(1)["state"] == "cancelled"
    assert env["pings"] == []  # no confusing 'finished' after a cancel
    assert env["inbox"].drain("discord:chan-9") == []


def test_artifact_problems_survive_a_long_report(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    long_report = ("x" * 3000) + "\nARTIFACT: missing.bin"
    env = runner_env(tmp_path, workspace=ws,
                     result=ClaudeResult(text=long_report, session_id=None, is_error=False))
    assert run_with(env) == 0
    # delivered in full to Discord (the skip note is never cut)
    discord = " ".join(t for _, t in env["pings"])
    assert "missing.bin" in discord
    # and it survives in the capped fold-back too, after the truncated report
    folded = env["inbox"].drain("discord:chan-9")[0]
    assert "missing.bin" in folded and "truncated" in folded


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


def test_spawn_runner_writes_a_per_job_log_not_devnull(tmp_path):
    import subprocess
    from iris.jobs import spawn_runner

    store = make_store(tmp_path)
    store.add("a", "x", [], "", "")
    captured = {}

    class FakeProc:
        pid = 999

    def fake_popen(*a, **k):
        captured.update(k)
        return FakeProc()

    spawn_runner(1, store=store, popen=fake_popen)
    # The runner's output goes to a real per-job log, not /dev/null, so a runner
    # that dies during import leaves a trace instead of vanishing silently.
    assert captured["stdout"] is not subprocess.DEVNULL
    assert captured["stderr"] == subprocess.STDOUT
    assert list(tmp_path.rglob("job-1.log")), "expected a per-job runner log file"


def test_spawn_runner_counts_start_attempts(tmp_path):
    from iris.jobs import spawn_runner

    store = make_store(tmp_path)
    store.add("a", "x", [], "", "")

    class FakeProc:
        pid = 1

    spawn_runner(1, store=store, popen=lambda *a, **k: FakeProc())
    spawn_runner(1, store=store, popen=lambda *a, **k: FakeProc())
    assert store.get(1)["spawn_attempts"] == 2


def test_retry_dead_starts_respawns_a_startup_death(tmp_path, monkeypatch):
    import iris.jobs as jobs_mod
    from iris.jobs import retry_dead_starts

    store = make_store(tmp_path)
    store.add("produce", "x", [], "", "")
    store.update(1, pid=4242, spawn_attempts=1)  # spawned once, then died importing
    store.add("queued", "y", [], "", "")  # never spawned: pid stays None
    monkeypatch.setattr(jobs_mod, "_pid_alive", lambda pid: False)
    respawned = []
    n = retry_dead_starts(store, spawn=lambda jid, **k: respawned.append(jid))
    assert n == 1 and respawned == [1]
    assert store.get(2)["state"] == "pending"  # queued job untouched


def test_retry_dead_starts_ignores_a_live_runner(tmp_path, monkeypatch):
    import iris.jobs as jobs_mod
    from iris.jobs import retry_dead_starts

    store = make_store(tmp_path)
    store.add("produce", "x", [], "", "")
    store.update(1, pid=4242, spawn_attempts=1)
    monkeypatch.setattr(jobs_mod, "_pid_alive", lambda pid: True)  # still importing/running
    assert retry_dead_starts(store, spawn=lambda *a, **k: None) == 0


def test_redeliver_reports_retries_a_failed_report_ping(tmp_path):
    from iris.config import Config
    from iris.jobs import redeliver_reports

    store = make_store(tmp_path)
    store.add("produce", "x", [], "", "chan-9")
    # a finished job whose report ping never landed (Discord was down at delivery)
    store.update(1, state="done", report="the result", report_delivered=False)
    sent = []
    n = redeliver_reports(Config(discord_token="tok"),
                          send=lambda c, t, k: sent.append((c, t)) or True, store=store)
    assert n == 1
    assert sent and sent[0][0] == "chan-9" and "the result" in sent[0][1]
    assert store.get(1)["report_delivered"] is True


def test_redeliver_reports_skips_a_just_finished_job_then_delivers(tmp_path):
    from iris.config import Config
    from iris.jobs import redeliver_reports

    store = make_store(tmp_path)
    store.add("produce", "x", [], "", "chan-9")
    store.update(1, state="done", report="r", report_delivered=False, finished_ts=1000.0)
    sent = []
    cfg = Config(discord_token="tok")
    # within the grace window: let the runner's own deliver() land first
    assert redeliver_reports(cfg, send=lambda *a: sent.append(1) or True, store=store, now=1050.0) == 0
    assert sent == []
    # past the grace window: the tick re-delivers
    assert redeliver_reports(cfg, send=lambda *a: sent.append(1) or True, store=store, now=2000.0) == 1


def test_redeliver_reports_gives_up_after_max_attempts(tmp_path):
    from iris.config import Config
    from iris.jobs import MAX_REDELIVER_ATTEMPTS, redeliver_reports

    store = make_store(tmp_path)
    store.add("produce", "x", [], "", "chan-9")
    store.update(1, state="done", report="r", report_delivered=False,
                 redeliver_attempts=MAX_REDELIVER_ATTEMPTS)  # already exhausted
    sent = []
    assert redeliver_reports(Config(discord_token="tok"),
                             send=lambda *a: sent.append(1) or True, store=store) == 0
    assert sent == []  # gave up; no further send attempts (report stays in the row + inbox)


def test_redeliver_reports_marks_an_empty_report_done(tmp_path):
    from iris.config import Config
    from iris.jobs import redeliver_reports

    store = make_store(tmp_path)
    store.add("produce", "x", [], "", "chan-9")
    store.update(1, state="done", report="", report_delivered=False)
    # nothing to send, but it must not stay pending forever
    assert redeliver_reports(Config(discord_token="tok"),
                             send=lambda *a: 1 / 0, store=store) == 0
    assert store.get(1)["report_delivered"] is True


def test_redeliver_reports_counts_a_failed_attempt(tmp_path):
    from iris.config import Config
    from iris.jobs import redeliver_reports

    store = make_store(tmp_path)
    store.add("produce", "x", [], "", "chan-9")
    store.update(1, state="done", report="r", report_delivered=False)
    assert redeliver_reports(Config(discord_token="tok"),
                             send=lambda *a: False, store=store) == 0  # send fails
    assert store.get(1)["report_delivered"] is False
    assert store.get(1)["redeliver_attempts"] == 1  # bounded retry counter advanced


def test_retry_dead_starts_stops_after_max_then_repair_fails_it(tmp_path, monkeypatch):
    import iris.jobs as jobs_mod
    from iris.jobs import MAX_START_ATTEMPTS, repair_dead_runners, retry_dead_starts

    store = make_store(tmp_path)
    store.add("produce", "x", [], "", "")
    store.update(1, pid=4242, spawn_attempts=MAX_START_ATTEMPTS)  # retries exhausted
    monkeypatch.setattr(jobs_mod, "_pid_alive", lambda pid: False)
    respawned = []
    assert retry_dead_starts(store, spawn=lambda jid, **k: respawned.append(jid)) == 0
    assert respawned == []
    # exhausted: the ordinary repair sweep then fails it so the owner is told once
    assert repair_dead_runners(store) == 1
    assert store.get(1)["state"] == "failed"


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
    folded = env["inbox"].drain("discord:chan-9")
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


def test_run_job_emits_job_metrics(tmp_path):
    env = runner_env(tmp_path, result=ClaudeResult(
        text="done", session_id="s", is_error=False, context_tokens=4321))
    metrics_path = tmp_path / "iris-metrics.json"
    env["config"].metrics_file = str(metrics_path)
    assert run_with(env) == 0
    lines = metrics_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["conversation_id"] == f"job:{env['job_id']}"
    assert rec["transport"] == "job"
    assert rec["context_tokens"] == 4321


def test_run_job_no_metrics_when_disabled(tmp_path):
    env = runner_env(tmp_path, result=ClaudeResult(
        text="done", session_id="s", is_error=False, context_tokens=10))
    env["config"].metrics_file = ""          # disabled -> emit is a no-op
    assert run_with(env) == 0
    assert not (tmp_path / "iris-metrics.json").exists()


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


def test_job_driver_inherits_the_trace_config(tmp_path):
    from iris.jobs import build_job_driver

    config = Config(jobs_enabled=True, trace_file=str(tmp_path / "trace.jsonl"),
                    trace_capture_content=True)
    driver = build_job_driver(config, {"grants": ["subagents"], "workspace": ""}, None)
    assert driver.trace_file == str(tmp_path / "trace.jsonl")
    assert driver.trace_kind == "job"  # so the ledger can tell jobs from chat
    assert driver.trace_capture_content is True


# -- sandbox cleanup (D3): no /tmp leak across scheduled runs ----------------


def test_write_browser_mcp_config_unlinks_on_write_failure(tmp_path, monkeypatch):
    import os

    import iris.jobs as jobs

    created = {}
    real_mkstemp = jobs.tempfile.mkstemp

    def record(*a, **k):
        fd, path = real_mkstemp(*a, **k)
        created["path"] = path
        return fd, path

    monkeypatch.setattr(jobs.tempfile, "mkstemp", record)
    monkeypatch.setattr(jobs.json, "dump", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    config = Config(jobs_enabled=True, browser_profile_dir=str(tmp_path / "p"))
    with pytest.raises(OSError):
        jobs.write_browser_mcp_config(config)
    assert not os.path.exists(created["path"])  # the half-written stub is removed


def test_build_job_driver_cleans_scratch_when_construction_fails(tmp_path, monkeypatch):
    import os

    import iris.jobs as jobs

    created = {}
    real_mkdtemp = jobs.tempfile.mkdtemp

    def record(*a, **k):
        d = real_mkdtemp(*a, **k)
        created["dir"] = d
        return d

    monkeypatch.setattr(jobs.tempfile, "mkdtemp", record)
    monkeypatch.setattr(jobs, "write_browser_mcp_config",
                        lambda c: (_ for _ in ()).throw(ValueError("bad browser cmd")))
    config = Config(jobs_enabled=True, browser_profile_dir=str(tmp_path / "p"))
    with pytest.raises(ValueError):
        jobs.build_job_driver(config, {"grants": ["browser"], "workspace": ""}, None)
    # the scratch dir allocated before the failure must not leak
    assert not os.path.exists(created["dir"])


def test_build_job_driver_tracks_scratch_cwd_for_cleanup(tmp_path):
    from iris.jobs import build_job_driver

    config = Config(jobs_enabled=True)
    driver = build_job_driver(config, {"grants": ["subagents"], "workspace": ""}, None)
    # A no-workspace job runs in a throwaway scratch dir that build owns.
    assert os.path.isdir(driver.cwd)
    assert driver.cwd in driver._job_temp_paths


def test_build_job_driver_does_not_track_a_workspace_dir(tmp_path):
    from iris.jobs import build_job_driver

    ws = tmp_path / "ws"
    ws.mkdir()
    driver = build_job_driver(Config(jobs_enabled=True),
                              {"grants": ["subagents"], "workspace": "ws"}, str(ws))
    # The workspace is the owner's; build must never list it for deletion.
    assert driver.cwd == str(ws)
    assert str(ws) not in driver._job_temp_paths
    assert driver._job_temp_paths == []


def test_build_job_driver_tracks_browser_mcp_config_for_cleanup(tmp_path):
    from iris.jobs import build_job_driver

    config = Config(jobs_enabled=True, job_grants=["browser"],
                    browser_profile_dir=str(tmp_path / "p"))
    driver = build_job_driver(config, {"grants": ["browser"], "workspace": ""}, None)
    assert os.path.exists(driver.mcp_config)
    assert driver.mcp_config in driver._job_temp_paths
    assert driver.cwd in driver._job_temp_paths  # scratch dir tracked too


def test_run_job_cleans_the_scratch_sandbox_on_completion(tmp_path):
    import tempfile

    env = runner_env(tmp_path, result=ClaudeResult(text="done", session_id="s", is_error=False))
    scratch = tempfile.mkdtemp(prefix="iris-job-test-")
    fd, cfg = tempfile.mkstemp(prefix="iris-job-mcp-test-", suffix=".json")
    os.close(fd)
    base = env["driver_factory"]

    def factory(config, job, workspace_path, child_pid_callback=None):
        drv = base(config, job, workspace_path, child_pid_callback)
        drv._job_temp_paths = [cfg, scratch]
        return drv

    env["driver_factory"] = factory
    assert run_with(env) == 0
    assert not os.path.exists(scratch)
    assert not os.path.exists(cfg)


def test_run_job_cleans_the_sandbox_even_when_the_turn_errors(tmp_path):
    import tempfile

    env = runner_env(tmp_path, result=ClaudeResult(text="", session_id="s", is_error=True, error="boom"))
    scratch = tempfile.mkdtemp(prefix="iris-job-test-")
    base = env["driver_factory"]

    def factory(config, job, workspace_path, child_pid_callback=None):
        drv = base(config, job, workspace_path, child_pid_callback)
        drv._job_temp_paths = [scratch]
        return drv

    env["driver_factory"] = factory
    assert run_with(env) == 1  # the turn failed
    assert not os.path.exists(scratch)  # ...but the sandbox is still cleaned


def test_run_job_does_not_delete_a_workspace_backed_dir(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "keep.txt").write_text("artifact", encoding="utf-8")
    env = runner_env(tmp_path, result=ClaudeResult(text="done", session_id="s", is_error=False),
                     workspace=ws)
    # A workspace-backed driver owns no scratch, so _job_temp_paths is empty.
    base = env["driver_factory"]

    def factory(config, job, workspace_path, child_pid_callback=None):
        drv = base(config, job, workspace_path, child_pid_callback)
        drv._job_temp_paths = []
        return drv

    env["driver_factory"] = factory
    assert run_with(env) == 0
    assert ws.exists() and (ws / "keep.txt").exists()


def test_run_job_delivers_a_long_report_in_full_across_messages(tmp_path):
    # The Discord ping must carry the WHOLE report (split across messages),
    # never cut; only the fold-back into the next chat turn stays capped.
    report = " ".join(f"line{i}" for i in range(700))  # ~5 KB, well over one message
    env = runner_env(tmp_path, result=ClaudeResult(text=report, session_id="s", is_error=False))
    assert run_with(env) == 0
    joined = " ".join(t for _, t in env["pings"])
    assert "line0" in joined and "line699" in joined          # head AND tail both delivered
    assert len(env["pings"]) >= 2                              # split into multiple messages
    assert all(len(t) <= 2000 for _, t in env["pings"])       # each under the Discord limit
    folded = env["inbox"].drain("discord:chan-9")
    assert len(folded) == 1 and len(folded[0]) <= 1600         # fold-back stays capped
