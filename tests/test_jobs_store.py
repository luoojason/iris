"""JobStore tests: persistence, claim atomicity, and cancel transitions."""

from __future__ import annotations

from iris.jobs import JobStore


def test_add_assigns_sequential_ids_and_get_returns_pending_record(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    first = store.add("refactor the parser", "refactor parser")
    second = store.add("write the docs", "docs")
    assert (first, second) == (1, 2)
    job = store.get(first)
    assert job["title"] == "refactor parser"
    assert job["prompt"] == "refactor the parser"
    assert job["status"] == "pending"
    assert job["started_at"] is None
    assert job["finished_at"] is None
    assert job["cancel_requested"] is False
    assert job["result"] is None


def test_add_without_timeout_records_the_default_1800(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("p", "t")  # timeout_s=None
    assert store.get(1)["timeout_s"] == 1800


def test_jobs_survive_reinstantiation_on_the_same_path(tmp_path):
    path = tmp_path / "jobs.json"
    JobStore(path).add("audit the deps", "deps audit", model="claude-haiku-4-5",
                       timeout_s=600, grants=["Task"], channel_id="c7",
                       conversation_id="discord:c7")
    job = JobStore(path).get(1)
    assert job is not None
    assert job["title"] == "deps audit"
    assert job["model"] == "claude-haiku-4-5"
    assert job["timeout_s"] == 600
    assert job["grants"] == ["Task"]
    assert job["channel_id"] == "c7"
    assert job["conversation_id"] == "discord:c7"


def test_corrupt_or_missing_file_reads_as_empty_and_ids_restart(tmp_path):
    path = tmp_path / "jobs.json"
    store = JobStore(path)
    assert store.all() == []
    path.write_text("{not json", "utf-8")
    assert store.all() == []
    assert store.add("p", "t") == 1  # max(ids)+1 over the empty load


def test_all_filters_by_status_and_sorts_by_id(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("one", "first")
    store.add("two", "second")
    store.update(1, status="done")
    assert [j["id"] for j in store.all()] == [1, 2]
    assert [j["id"] for j in store.all(status="pending")] == [2]
    assert [j["id"] for j in store.all(status="done")] == [1]


def test_update_sets_fields_and_reports_missing_ids(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("p", "t")
    assert store.update(1, status="failed", finished_at=42.0,
                        result={"text": "boom", "is_error": True}) is True
    job = store.get(1)
    assert job["status"] == "failed"
    assert job["finished_at"] == 42.0
    assert job["result"]["text"] == "boom"
    assert store.update(99, status="done") is False


def test_claim_pending_flips_to_running_and_stamps_started_at(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("one", "first")
    claimed = store.claim_pending(5, now=123.0)
    assert [j["id"] for j in claimed] == [1]
    assert claimed[0]["status"] == "running"
    assert claimed[0]["started_at"] == 123.0
    on_disk = store.get(1)
    assert on_disk["status"] == "running"  # the flip hit disk before returning
    assert on_disk["started_at"] == 123.0


def test_second_claim_returns_nothing_for_already_claimed_jobs(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("one", "first")
    store.add("two", "second")
    assert [j["id"] for j in store.claim_pending(5)] == [1, 2]
    assert store.claim_pending(5) == []  # atomic: nothing claimed twice


def test_claim_pending_respects_the_limit_oldest_first(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("one", "first")
    store.add("two", "second")
    store.add("three", "third")
    assert [j["id"] for j in store.claim_pending(2)] == [1, 2]
    assert [j["id"] for j in store.claim_pending(2)] == [3]


def test_claim_pending_with_no_free_slots_claims_nothing(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("one", "first")
    assert store.claim_pending(0) == []
    assert store.get(1)["status"] == "pending"


def test_cancel_of_pending_job_cancels_outright(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("p", "t")
    assert store.request_cancel(1) == "Cancelled job #1."
    job = store.get(1)
    assert job["status"] == "cancelled"
    assert job["cancel_requested"] is False  # nothing is running to stop


def test_cancel_of_running_job_asks_the_runner_to_stop(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("p", "t")
    store.claim_pending(1)
    assert store.request_cancel(1) == "Asked the runner to stop job #1."
    job = store.get(1)
    assert job["status"] == "running"  # the runner flips it once the kill lands
    assert job["cancel_requested"] is True


def test_cancel_of_finished_jobs_says_already_finished(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    for status in ("done", "failed", "cancelled", "interrupted"):
        jid = store.add("p", status)
        store.update(jid, status=status)
        assert store.request_cancel(jid) == f"Job #{jid} already finished."
        assert store.get(jid)["status"] == status  # untouched


def test_cancel_of_missing_job_is_a_friendly_string(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    assert store.request_cancel(7) == "No job #7."
