"""Tests for the autonomous-resume queue, budget, and dispatch decision."""

from __future__ import annotations

from iris.autoresume import ResumeBudget, ResumeQueue, dispatch_resumes


def test_queue_enqueue_then_drain_roundtrips(tmp_path):
    q = ResumeQueue(str(tmp_path / "resume.json"))
    q.enqueue("discord:123", "task A finished")
    q.enqueue("discord:123", "task B finished")
    items = q.drain()
    assert [i["conversation_id"] for i in items] == ["discord:123", "discord:123"]
    assert [i["prompt"] for i in items] == ["task A finished", "task B finished"]
    assert q.drain() == []  # drain empties


def test_queue_drain_missing_file_is_empty(tmp_path):
    assert ResumeQueue(str(tmp_path / "nope.json")).drain() == []


def test_queue_caps_oldest(tmp_path):
    q = ResumeQueue(str(tmp_path / "resume.json"))
    for i in range(ResumeQueue.CAP + 5):
        q.enqueue("discord:1", f"note {i}")
    items = q.drain()
    assert len(items) == ResumeQueue.CAP
    assert items[-1]["prompt"] == f"note {ResumeQueue.CAP + 4}"  # newest kept


def test_budget_allows_up_to_cap_then_refuses(tmp_path):
    b = ResumeBudget(str(tmp_path / "state.json"), cap=2)
    now = 1_000_000.0
    assert b.take(now) is True
    assert b.take(now) is True
    assert b.take(now) is False  # third in the same day refused


def test_budget_resets_on_new_utc_day(tmp_path):
    b = ResumeBudget(str(tmp_path / "state.json"), cap=1)
    day1 = 1_000_000.0
    assert b.take(day1) is True
    assert b.take(day1) is False
    assert b.take(day1 + 86_400) is True  # next day, fresh allowance


def test_budget_zero_cap_never_allows(tmp_path):
    b = ResumeBudget(str(tmp_path / "state.json"), cap=0)
    assert b.take(1_000_000.0) is False


def test_dispatch_fires_each_accepted_request(tmp_path):
    q = ResumeQueue(str(tmp_path / "resume.json"))
    q.enqueue("discord:1", "A")
    q.enqueue("discord:2", "B")
    b = ResumeBudget(str(tmp_path / "state.json"), cap=10)
    fired = []
    n = dispatch_resumes(q, b, now=1.0, parked=False,
                         submit=lambda conv, prompt: fired.append((conv, prompt)))
    assert n == 2
    assert fired == [("discord:1", "A"), ("discord:2", "B")]
    assert q.drain() == []  # consumed


def test_dispatch_drops_everything_when_parked(tmp_path):
    q = ResumeQueue(str(tmp_path / "resume.json"))
    q.enqueue("discord:1", "A")
    b = ResumeBudget(str(tmp_path / "state.json"), cap=10)
    fired = []
    n = dispatch_resumes(q, b, now=1.0, parked=True,
                         submit=lambda conv, prompt: fired.append((conv, prompt)))
    assert n == 0
    assert fired == []
    assert b.take(1.0) is True  # park did not spend the budget


def test_dispatch_stops_at_budget_cap(tmp_path):
    q = ResumeQueue(str(tmp_path / "resume.json"))
    q.enqueue("discord:1", "A")
    q.enqueue("discord:2", "B")
    q.enqueue("discord:3", "C")
    b = ResumeBudget(str(tmp_path / "state.json"), cap=2)
    fired = []
    n = dispatch_resumes(q, b, now=1.0, parked=False,
                         submit=lambda conv, prompt: fired.append((conv, prompt)))
    assert n == 2
    assert [c for c, _ in fired] == ["discord:1", "discord:2"]


def test_dispatch_skips_malformed_items(tmp_path):
    q = ResumeQueue(str(tmp_path / "resume.json"))
    q.enqueue("", "no channel")
    q.enqueue("discord:1", "")
    q.enqueue("discord:2", "ok")
    b = ResumeBudget(str(tmp_path / "state.json"), cap=10)
    fired = []
    n = dispatch_resumes(q, b, now=1.0, parked=False,
                         submit=lambda conv, prompt: fired.append((conv, prompt)))
    assert n == 1
    assert fired == [("discord:2", "ok")]
