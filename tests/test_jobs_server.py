"""Jobs MCP server tests: friendly-string tools over a real JobStore file.

The server is a registry writer only (it never spawns anything), so every
tool is exercised against a JobStore under tmp_path via the monkeypatched
``srv.STORE`` seam; ``srv._now`` is the module's one clock, monkeypatched
for the duplicate-spawn window and age rendering.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")  # the server needs the MCP SDK; skip if absent

from iris.jobs import JobStore
from iris.mcp import jobs_server as srv


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = JobStore(tmp_path / "jobs.json")
    monkeypatch.setattr(srv, "STORE", s)
    return s


def test_spawn_job_queues_a_pending_job_with_store_defaults(store):
    out = srv.spawn_job("refactor the parser end to end", title="refactor parser")
    assert out == "Job #1 queued: refactor parser"
    job = store.get(1)
    assert job["status"] == "pending"
    assert job["prompt"] == "refactor the parser end to end"
    assert job["title"] == "refactor parser"
    assert job["timeout_s"] == 1800  # timeout_minutes=0 -> store default
    assert job["grants"] == []
    assert job["model"] == ""


def test_spawn_job_without_prompt_returns_a_guard_string(store):
    out = srv.spawn_job("   ")
    assert "prompt" in out.lower()
    assert store.all() == []  # nothing queued


def test_spawn_job_converts_and_clamps_timeout_minutes(store):
    srv.spawn_job("p1", title="ten minutes", timeout_minutes=10)
    assert store.get(1)["timeout_s"] == 600
    srv.spawn_job("p2", title="too long", timeout_minutes=999)
    assert store.get(2)["timeout_s"] == 240 * 60  # clamped to 4 hours


def test_spawn_job_accepts_task_and_agent_grants(store):
    srv.spawn_job("p", title="fan out", grants="Task")
    assert store.get(1)["grants"] == ["Task"]
    srv.spawn_job("p2", title="fan out 2", grants="Agent, Task")
    assert store.get(2)["grants"] == ["Agent", "Task"]


def test_spawn_job_rejects_unknown_grants_listing_valid_names(store):
    out = srv.spawn_job("p", title="bad grant", grants="Foo")
    assert "Foo" in out
    assert "Task" in out and "Agent" in out  # the valid names are listed
    assert store.all() == []  # nothing queued


def test_spawn_job_refuses_a_near_duplicate_inside_the_window(store, monkeypatch):
    first = srv.spawn_job("audit deps", title="deps")
    assert first == "Job #1 queued: deps"
    created = store.get(1)["created_at"]
    monkeypatch.setattr(srv, "_now", lambda: created + 4.0)  # inside 5s window
    out = srv.spawn_job("audit deps", title="deps")
    assert out == "Job #1 already queued: deps"
    assert len(store.all()) == 1  # the retry did not double-queue


def test_spawn_job_allows_the_same_job_again_after_the_window(store, monkeypatch):
    srv.spawn_job("audit deps", title="deps")
    created = store.get(1)["created_at"]
    monkeypatch.setattr(srv, "_now", lambda: created + 6.0)  # window expired
    out = srv.spawn_job("audit deps", title="deps")
    assert out == "Job #2 queued: deps"
    assert len(store.all()) == 2


def test_spawn_job_duplicate_guard_ignores_non_pending_twins(store, monkeypatch):
    srv.spawn_job("audit deps", title="deps")
    created = store.get(1)["created_at"]
    store.claim_pending(1)  # the twin is running now, not pending
    monkeypatch.setattr(srv, "_now", lambda: created + 1.0)
    out = srv.spawn_job("audit deps", title="deps")
    assert out == "Job #2 queued: deps"


def test_list_jobs_on_an_empty_store_says_no_jobs(store):
    assert srv.list_jobs() == "No jobs."


def test_list_jobs_rejects_an_unknown_status_naming_the_valid_ones(store):
    out = srv.list_jobs("bogus")
    assert "bogus" in out
    for valid in ("pending", "running", "done", "failed", "cancelled", "interrupted"):
        assert valid in out


def test_list_jobs_renders_ages_newest_first(store, monkeypatch):
    store.add("one", "refactor parser")
    store.add("two", "write docs")
    store.update(1, status="running", started_at=10_000.0, created_at=9_000.0)
    store.update(2, created_at=10_000.0)  # pending: age from created_at
    monkeypatch.setattr(srv, "_now", lambda: 10_240.0)  # 4m after both stamps
    lines = srv.list_jobs().splitlines()
    assert lines == ["#2 [pending 4m] write docs", "#1 [running 4m] refactor parser"]


def test_list_jobs_uses_hour_units_past_an_hour_and_filters_by_status(store, monkeypatch):
    store.add("one", "refactor parser")
    store.add("two", "write docs")
    store.update(1, status="running", started_at=0.0)
    store.update(2, created_at=0.0)
    monkeypatch.setattr(srv, "_now", lambda: 7_500.0)  # just past 2h
    assert srv.list_jobs("running") == "#1 [running 2h] refactor parser"
    assert srv.list_jobs("done") == "No done jobs."


def test_job_status_of_a_missing_job_is_a_friendly_string(store):
    assert srv.job_status(7) == "No job #7."


def test_job_status_renders_the_full_detail_for_a_failed_job(store, monkeypatch):
    store.add("p", "deps audit", model="claude-haiku-4-5", grants=["Task"])
    store.update(1, status="failed", created_at=0.0, started_at=60.0,
                 finished_at=300.0, cancel_requested=True,
                 result={"text": "", "is_error": True, "error": "turn timed out"})
    monkeypatch.setattr(srv, "_now", lambda: 600.0)
    out = srv.job_status(1)
    assert "Job #1: deps audit" in out
    assert "status: failed" in out
    assert "created 10m ago" in out
    assert "started 9m ago" in out
    assert "finished 5m ago" in out
    assert "model: claude-haiku-4-5" in out
    assert "grants: Task" in out
    assert "cancel requested" in out
    assert "error: turn timed out" in out


def test_job_status_of_a_pending_job_skips_absent_fields(store, monkeypatch):
    store.add("p", "deps audit")
    store.update(1, created_at=0.0)
    monkeypatch.setattr(srv, "_now", lambda: 120.0)
    out = srv.job_status(1)
    assert "status: pending" in out
    assert "created 2m ago" in out
    assert "started" not in out
    assert "finished" not in out
    assert "model" not in out
    assert "grants" not in out
    assert "cancel" not in out
    assert "error" not in out


def test_cancel_job_passes_the_store_outcome_through(store):
    store.add("p", "pending one")
    store.add("p", "running one")
    store.claim_pending(0)  # no-op: nothing claimed
    assert srv.cancel_job(1) == "Cancelled job #1."
    store.claim_pending(1)  # claims #2 (the lowest remaining pending id)
    assert srv.cancel_job(2) == "Asked the runner to stop job #2."
    assert store.get(2)["cancel_requested"] is True
    assert srv.cancel_job(9) == "No job #9."


def test_job_result_of_a_missing_job_is_a_friendly_string(store):
    assert srv.job_result(3) == "No job #3."


def test_job_result_before_finish_says_no_result_yet(store):
    store.add("p", "t")
    assert srv.job_result(1) == "Job #1 is pending (no result yet)."
    store.claim_pending(1)
    assert srv.job_result(1) == "Job #1 is running (no result yet)."


def test_job_result_of_a_failed_job_returns_the_error_text(store):
    store.add("p", "t")
    store.update(1, status="failed",
                 result={"text": "", "is_error": True, "error": "turn timed out"})
    out = srv.job_result(1)
    assert "failed" in out
    assert "turn timed out" in out


def test_job_result_returns_the_stored_text_unclamped_when_short(store):
    store.add("p", "t")
    store.update(1, status="done", result={"text": "all twelve modules pass"})
    assert srv.job_result(1) == "all twelve modules pass"


def test_job_result_clamps_to_max_chars_with_a_floor_of_200(store):
    store.add("p", "t")
    store.update(1, status="done", result={"text": "x" * 500})
    out = srv.job_result(1, max_chars=50)  # below the floor -> clamped up to 200
    assert out.startswith("x" * 200)
    assert "x" * 201 not in out
    assert "truncated" in out


def test_job_result_caps_max_chars_at_20000(store):
    store.add("p", "t")
    store.update(1, status="done", result={"text": "y" * 25_000})
    out = srv.job_result(1, max_chars=99_999)
    assert out.startswith("y" * 20_000)
    assert "y" * 20_001 not in out
    assert "truncated" in out


def test_job_result_of_a_cancelled_job_without_text_says_so(store):
    store.add("p", "t")
    store.update(1, status="cancelled", result=None)
    out = srv.job_result(1)
    assert "cancelled" in out
    assert "no result" in out.lower()


def test_spawn_list_status_cancel_result_round_trip_on_one_file(tmp_path, monkeypatch):
    real = JobStore(tmp_path / "jobs.json")
    monkeypatch.setattr(srv, "STORE", real)
    assert srv.spawn_job("profile the importer", title="profile importer",
                         grants="Task", timeout_minutes=20) \
        == ("Job #1 queued: profile importer (grants recorded; the runner "
            "applies the operator ceiling)")
    assert "#1 [pending" in srv.list_jobs()
    assert "status: pending" in srv.job_status(1)

    real.claim_pending(1)  # the bot-side runner picks it up
    assert "#1 [running" in srv.list_jobs("running")
    assert srv.cancel_job(1) == "Asked the runner to stop job #1."
    assert "cancel requested" in srv.job_status(1)

    # the runner kills the turn, records the outcome, and the result lands
    real.update(1, status="cancelled", finished_at=real.get(1)["started_at"] + 1,
                result={"text": "partial notes survived", "is_error": True,
                        "error": "cancelled"})
    assert srv.job_result(1) == "partial notes survived"
    assert "status: cancelled" in srv.job_status(1)


def test_spawn_with_grants_notes_the_operator_ceiling(store):
    out = srv.spawn_job("fan out the analysis", grants="Task")
    assert "queued" in out
    assert "operator ceiling" in out


def test_spawn_without_grants_keeps_the_plain_reply(store):
    out = srv.spawn_job("plain work")
    assert out == "Job #1 queued: plain work"
