"""JobRunner lifecycle tests against a scripted stream driver (no claude).

Seams: stream_driver_factory swaps in FakeStreamDriver/FakeTurn (modeled on
tests/test_stream_cancel.py and the FakeDriver in tests/test_agent.py),
sync=True runs workers inline, deliver/sender capture both delivery paths,
and runner.workers / runner.turn_registered bound every thread join. Fakes
are local: no conftest.
"""

from __future__ import annotations

import logging
import threading

import pytest

from iris.driver import ClaudeDriver, ClaudeResult
from iris.jobs import JobRunner, JobStore


def ok_result(text="all done", sid="job-sess"):
    return ClaudeResult(text=text, session_id=sid, is_error=False,
                        cost_usd=0.01, duration_ms=1200, context_tokens=900)


def err_result(error="boom"):
    return ClaudeResult(text="", session_id=None, is_error=True, error=error)


class FakeTurn:
    """Scripted StreamTurn honoring the cancel contract from test_stream_cancel:
    cancel() is True only for a live turn; cancel before the primary swaps in
    an is_error result with "cancelled" in it; a landed ok reply survives a
    later cancel untouched."""

    def __init__(self, result, *, hold=False, landed=False):
        self._result = result
        self._landed = landed or not hold
        self._live = hold
        self._gate = threading.Event()
        if not hold:
            self._gate.set()
        self.killed = False
        self.cancel_calls = 0

    def release(self):
        self._live = False
        self._gate.set()

    def wait_primary(self, timeout=None):
        self._gate.wait(2)
        return self._result

    def wait_finished(self, timeout=None):
        return self._gate.wait(2)

    def cancel(self):
        self.cancel_calls += 1
        if not self._live:
            return False
        self._live = False
        self.killed = True
        if not self._landed:
            self._result = ClaudeResult(text="", session_id=None, is_error=True,
                                        error="claude stream turn cancelled")
        self._gate.set()
        return True


class FakeStreamDriver:
    """Hands out queued FakeTurns and records start() calls (FakeDriver-shaped)."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = []

    def start(self, prompt, session_id=None, model=None):
        self.calls.append((prompt, session_id, model))
        return self.turns.pop(0)


def collect_sender(sent):
    def sender(channel, text, token):
        sent.append((channel, text, token))
        return True
    return sender


def make_runner(store, turns, *, captured=None, **kw):
    """A JobRunner on a FakeStreamDriver; returns (runner, fake_stream_driver)."""
    sd = FakeStreamDriver(turns)

    def factory(job_driver, *, idle_timeout, total_timeout):
        if captured is not None:
            captured.append((job_driver, idle_timeout, total_timeout))
        return sd

    kw.setdefault("sync", True)
    kw.setdefault("notify_channel", "999")
    kw.setdefault("discord_token", "tok")
    return JobRunner(store, ClaudeDriver(), stream_driver_factory=factory, **kw), sd


# -- lifecycle -------------------------------------------------------------


def test_done_job_persists_result_status_and_timestamps(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    jid = store.add("summarize the repo", "summary")
    sent = []
    runner, _ = make_runner(store, [FakeTurn(ok_result())], sender=collect_sender(sent))

    runner.check_now()

    job = store.get(jid)
    assert job["status"] == "done"
    assert job["result"]["text"] == "all done"
    assert job["result"]["session_id"] == "job-sess"
    assert job["result"]["is_error"] is False
    assert job["result"]["cost_usd"] == 0.01
    assert job["started_at"] is not None
    assert job["finished_at"] is not None


def test_failed_job_persists_the_error(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    jid = store.add("doomed work", "doomed")
    sent = []
    runner, _ = make_runner(store, [FakeTurn(err_result("boom"))],
                            sender=collect_sender(sent))

    runner.check_now()

    job = store.get(jid)
    assert job["status"] == "failed"
    assert job["result"]["is_error"] is True
    assert job["result"]["error"] == "boom"


def test_worker_starts_a_fresh_session_with_the_jobs_prompt_and_model(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("translate the docs", "docs", model="claude-haiku-4-5")
    runner, sd = make_runner(store, [FakeTurn(ok_result())], sender=collect_sender([]))

    runner.check_now()

    # Fresh session (None), the job's prompt, the job's model override.
    assert sd.calls == [("translate the docs", None, "claude-haiku-4-5")]


def test_empty_job_model_is_passed_as_none(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("plain work", "plain")
    runner, sd = make_runner(store, [FakeTurn(ok_result())], sender=collect_sender([]))

    runner.check_now()

    assert sd.calls == [("plain work", None, None)]


def test_grant_ceiling_and_timeouts_flow_into_the_stream_factory(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("fan out", "fanout", grants=["Task", "Bash"], timeout_s=900)
    captured = []
    runner, _ = make_runner(store, [FakeTurn(ok_result())], captured=captured,
                            sender=collect_sender([]), idle_timeout=120.0)

    runner.check_now()

    job_driver, idle_timeout, total_timeout = captured[0]
    assert idle_timeout == 120.0
    assert total_timeout == 900.0
    # Task granted within the ceiling (and the Agent alias rides along); Bash
    # asked for but outside the default ("Task",) ceiling stays denied.
    assert "Task" not in job_driver.disallowed_tools
    assert "Agent" not in job_driver.disallowed_tools
    assert "Bash" in job_driver.disallowed_tools


# -- delivery ----------------------------------------------------------------


def test_stamped_job_delivers_via_callback_and_skips_the_spine(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    jid = store.add("research", "deep dive", channel_id="42",
                    conversation_id="discord:42")
    delivered, sent = [], []

    def deliver(channel_id, conversation_id, text):
        delivered.append((channel_id, conversation_id, text))
        return True

    runner, _ = make_runner(store, [FakeTurn(ok_result(text="the findings"))],
                            deliver=deliver, sender=collect_sender(sent))
    runner.check_now()

    expected = f'[background job #{jid} "deep dive" finished]\nthe findings'
    assert delivered == [("42", "discord:42", expected)]
    assert sent == []  # fold-back delivered: never both paths for one job


def test_failed_stamped_job_folds_back_with_the_failure_shape(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    jid = store.add("research", "deep dive", channel_id="42",
                    conversation_id="discord:42")
    delivered, sent = [], []

    def deliver(channel_id, conversation_id, text):
        delivered.append((channel_id, conversation_id, text))
        return True

    runner, _ = make_runner(store, [FakeTurn(err_result("exploded"))],
                            deliver=deliver, sender=collect_sender(sent))
    runner.check_now()

    assert delivered == [
        ("42", "discord:42", f'[background job #{jid} "deep dive" failed: exploded]')
    ]
    assert sent == []


def test_deliver_returning_false_falls_back_to_the_spine_on_the_job_channel(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("work", "title", channel_id="42", conversation_id="discord:42")
    sent = []
    runner, _ = make_runner(store, [FakeTurn(ok_result())],
                            deliver=lambda *a: False, sender=collect_sender(sent))

    runner.check_now()

    assert len(sent) == 1
    channel, text, token = sent[0]
    assert channel == "42"  # the job's channel wins over notify_channel
    assert token == "tok"
    assert text.startswith("job done: title in")


def test_unstamped_job_skips_the_callback_and_pings_the_notify_channel(tmp_path):
    # Quick success: duration is ~0s, far under watch_min_seconds, but the
    # spine ping is forced because the owner explicitly asked for the job.
    store = JobStore(tmp_path / "jobs.json")
    store.add("work", "title")
    delivered, sent = [], []
    runner, _ = make_runner(
        store, [FakeTurn(ok_result())],
        deliver=lambda *a: delivered.append(a) or True,
        sender=collect_sender(sent), watch_min_seconds=30.0,
    )

    runner.check_now()

    assert delivered == []  # unstamped: the fold-back callback is never tried
    assert sent == [("999", sent[0][1], "tok")]
    assert sent[0][1].startswith("job done: title in")


def test_failure_uses_the_one_shot_notify_driver_when_a_factory_is_given(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("deploy", "deploy")
    sent = []

    class FakeNotifyDriver:
        def __init__(self):
            self.prompts = []

        def run(self, prompt, session_id=None, model=None):
            self.prompts.append(prompt)
            return ClaudeResult(text="The deploy broke, sir.", session_id=None,
                                is_error=False)

    nd = FakeNotifyDriver()
    runner, _ = make_runner(store, [FakeTurn(err_result("ERR: exploded"))],
                            sender=collect_sender(sent),
                            notify_driver_factory=lambda: nd)
    runner.check_now()

    assert sent == [("999", "The deploy broke, sir.", "tok")]
    assert "A background job" in nd.prompts[0]  # the job-flavored prompt
    assert "ERR: exploded" in nd.prompts[0]     # the tail reached triage


def test_failure_without_a_factory_stays_on_the_template(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("deploy", "deploy")
    sent = []
    runner, _ = make_runner(store, [FakeTurn(err_result("ERR: exploded"))],
                            sender=collect_sender(sent))

    runner.check_now()

    assert len(sent) == 1
    assert sent[0][1].startswith("job failed: deploy exited 1 after")


def test_failure_tail_is_clamped_to_the_last_25_lines(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("noisy", "noisy")
    error = "\n".join(f"line {n}" for n in range(40))
    prompts = []

    class FakeNotifyDriver:
        def run(self, prompt, session_id=None, model=None):
            prompts.append(prompt)
            return ClaudeResult(text="triaged", session_id=None, is_error=False)

    runner, _ = make_runner(store, [FakeTurn(err_result(error))],
                            sender=collect_sender([]),
                            notify_driver_factory=FakeNotifyDriver)
    runner.check_now()

    assert "line 39" in prompts[0] and "line 15" in prompts[0]
    assert "line 14\n" not in prompts[0]


# -- cancel --------------------------------------------------------------


def test_cancel_requested_kills_the_turn_and_records_cancelled(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    jid = store.add("long haul", "long")
    turn = FakeTurn(ok_result(), hold=True)
    sent = []
    runner, _ = make_runner(store, [turn], sync=False, sender=collect_sender(sent))

    runner.check_now()  # claims and spawns the worker
    assert runner.turn_registered.wait(timeout=2)
    assert store.request_cancel(jid).startswith("Asked the runner")
    runner.check_now()  # the cancel pass kills the live turn

    runner.workers[jid].join(timeout=2)
    assert not runner.workers[jid].is_alive()
    assert turn.killed is True
    job = store.get(jid)
    assert job["status"] == "cancelled"
    assert job["result"]["is_error"] is True
    assert "cancelled" in job["result"]["error"]
    assert sent == []  # cancelled jobs skip delivery entirely


def test_cancel_after_the_ok_primary_landed_keeps_done(tmp_path):
    # Per the StreamTurn contract, cancel() after the reply landed kills the
    # leftover process but preserves the stored ok result: the job is done.
    store = JobStore(tmp_path / "jobs.json")
    jid = store.add("quick win", "win")
    turn = FakeTurn(ok_result(text="the answer"), hold=True, landed=True)
    sent = []
    runner, _ = make_runner(store, [turn], sync=False, sender=collect_sender(sent))

    runner.check_now()
    assert runner.turn_registered.wait(timeout=2)
    store.request_cancel(jid)
    runner.check_now()

    runner.workers[jid].join(timeout=2)
    assert turn.killed is True  # the live process was still reaped
    job = store.get(jid)
    assert job["status"] == "done"
    assert job["result"]["text"] == "the answer"
    assert len(sent) == 1  # a done job still delivers


def test_runner_cancel_is_recorded_even_without_the_sentinel_error_text(tmp_path):
    # PIN: the _cancel_flagged flag alone decides "cancelled"; the error text
    # is free-form (model prose, folded stderr) and must not be load-bearing.
    store = JobStore(tmp_path / "jobs.json")
    jid = store.add("long haul", "long")
    turn = FakeTurn(err_result("killed by signal"), hold=True, landed=True)
    sent = []
    runner, _ = make_runner(store, [turn], sync=False, sender=collect_sender(sent))

    runner.check_now()
    assert runner.turn_registered.wait(timeout=2)
    store.request_cancel(jid)
    runner.check_now()

    runner.workers[jid].join(timeout=2)
    job = store.get(jid)
    assert job["status"] == "cancelled"
    assert sent == []


def test_a_failure_mentioning_cancelled_is_recorded_failed_and_pinged(tmp_path):
    # An uncancelled failure whose error text happens to contain the word
    # "cancelled" (e.g. folded stderr "request was cancelled by the server")
    # is a real failure: the owner must get the ping, and the registry must
    # not show a cancel nobody asked for.
    store = JobStore(tmp_path / "jobs.json")
    jid = store.add("deploy", "deploy")
    sent = []
    runner, _ = make_runner(
        store, [FakeTurn(err_result("request was cancelled by the server"))],
        sender=collect_sender(sent))

    runner.check_now()

    job = store.get(jid)
    assert job["status"] == "failed"
    assert len(sent) == 1
    assert sent[0][1].startswith("job failed: deploy")


# -- robustness ----------------------------------------------------------------


def test_a_claim_pending_failure_releases_the_drained_permits(tmp_path):
    # A transient registry write error (disk full, unwritable lock file) must
    # not eat semaphore permits: a leaked permit is gone for the life of the
    # process and silently shrinks the runner's capacity to zero.
    store = JobStore(tmp_path / "jobs.json")
    jid = store.add("work", "title")
    runner, _ = make_runner(store, [FakeTurn(ok_result())], sender=collect_sender([]))
    real = store.claim_pending
    failed_once = []

    def flaky(limit, now=None):
        if not failed_once:
            failed_once.append(1)
            raise OSError("disk full")
        return real(limit, now)

    store.claim_pending = flaky
    with pytest.raises(OSError):
        runner.check_now()

    runner.check_now()  # every permit came back: the job is claimed and runs

    assert store.get(jid)["status"] == "done"


def test_the_watcher_survives_a_check_now_that_raises(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    runner, _ = make_runner(store, [], sync=False, poll_seconds=0.01,
                            sender=collect_sender([]))
    first, second = threading.Event(), threading.Event()

    def boom():
        if not first.is_set():
            first.set()
            raise OSError("disk full")
        second.set()

    runner.check_now = boom
    runner.start()
    try:
        store.add("one", "one")    # first mtime change: check raises
        assert first.wait(timeout=2)
        assert runner.watcher.is_alive()
        store.add("two", "two")    # the watcher must still be checking
        assert second.wait(timeout=2)
    finally:
        runner.stop()


def test_a_registry_failure_in_turn_finished_never_raises(tmp_path):
    # The adapter brackets call turn_finished on the chat turn's exit path; a
    # raise there would replace the reply the model call already paid for.
    store = JobStore(tmp_path / "jobs.json")
    runner, _ = make_runner(store, [FakeTurn(ok_result())], sender=collect_sender([]))
    runner.turn_started("discord:7", "7")
    store.add("mid-turn work", "midturn")

    def exploding_update(job_id, **fields):
        raise OSError("disk full")

    store.update = exploding_update
    runner.turn_finished("discord:7")  # must not raise


def test_a_crashing_worker_records_a_failed_outcome(tmp_path):
    # An exception escaping _run_job (here: the stream factory) must never
    # strand the job as "running" forever with a silently dead worker.
    store = JobStore(tmp_path / "jobs.json")
    jid = store.add("doomed", "doomed")
    sent = []

    def exploding_factory(job_driver, *, idle_timeout, total_timeout):
        raise RuntimeError("factory exploded")

    runner = JobRunner(store, ClaudeDriver(), stream_driver_factory=exploding_factory,
                       sync=True, notify_channel="999", discord_token="tok",
                       sender=collect_sender(sent))

    runner.check_now()

    job = store.get(jid)
    assert job["status"] == "failed"
    assert "factory exploded" in job["result"]["error"]
    assert len(sent) == 1  # the failure still pings the spine


def test_an_undeliverable_spine_ping_is_logged(tmp_path, caplog):
    # The spine is the terminal fallback; when even it cannot deliver, the
    # miss must leave a trace instead of evaporating.
    store = JobStore(tmp_path / "jobs.json")
    store.add("work", "title")
    runner, _ = make_runner(store, [FakeTurn(ok_result())],
                            sender=lambda channel, text, token: False)

    with caplog.at_level(logging.WARNING, logger="iris.jobs"):
        runner.check_now()

    assert any("undeliverable" in r.getMessage() for r in caplog.records)


def test_cancel_landing_between_claim_and_registration_is_honored(tmp_path):
    # request_cancel can arrive while the worker is mid-spawn: the record is
    # already running but the turn is not yet registered, so the cancel pass
    # cannot see it and no later store change re-triggers it. The worker must
    # re-read the record once the turn registers.
    store = JobStore(tmp_path / "jobs.json")
    jid = store.add("long work", "long work")
    turn = FakeTurn(ok_result(), hold=True)

    def racing_factory(job_driver, *, idle_timeout, total_timeout):
        sd = FakeStreamDriver([turn])
        orig = sd.start

        def start(prompt, session_id=None, model=None):
            store.request_cancel(jid)  # owner cancels during the spawn window
            return orig(prompt, session_id, model)

        sd.start = start
        return sd

    runner = JobRunner(store, ClaudeDriver(), stream_driver_factory=racing_factory,
                       sync=True)
    runner.check_now()

    assert turn.killed
    assert store.get(jid)["status"] == "cancelled"


# -- credit guard: parking ------------------------------------------------------


def budget_kw(tmp_path, **over):
    kw = dict(budget_state_path=str(tmp_path / "budget.json"), park_minutes=60.0)
    kw.update(over)
    return kw


def park_pings(sent):
    return [text for _, text, _ in sent if text.startswith("jobs parked until ~")]


def test_credit_pushback_parks_claiming_until_expiry(tmp_path):
    import time as _time

    from iris.budget import BudgetState

    store = JobStore(tmp_path / "jobs.json")
    doomed = store.add("doomed", "doomed")
    sent = []
    runner, sd = make_runner(
        store, [FakeTurn(err_result("Credit balance is too low"))],
        sender=collect_sender(sent), concurrency=1, **budget_kw(tmp_path))

    runner.check_now()

    assert store.get(doomed)["status"] == "failed"
    state_path = tmp_path / "budget.json"
    assert BudgetState(state_path).park_until > _time.time() + 3000  # ~60m out
    pings = park_pings(sent)
    assert len(pings) == 1
    assert "the credit pool or rate limit pushed back" in pings[0]

    # While parked: pending jobs stay queued, no claim happens (an attempted
    # claim would pop the empty FakeStreamDriver queue and fail loudly).
    queued = store.add("queued work", "queued")
    runner.check_now()
    assert store.get(queued)["status"] == "pending"

    # Expiry: one templated resume ping, the park clears, claiming resumes.
    BudgetState(state_path).set_park_until(_time.time() - 1)
    sd.turns.append(FakeTurn(ok_result()))
    runner.check_now()
    assert store.get(queued)["status"] == "done"
    resumes = [t for _, t, _ in sent if t.startswith("jobs resumed")]
    assert len(resumes) == 1
    assert BudgetState(state_path).park_until == 0.0


def test_a_second_pushback_while_parked_pings_only_once(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("one", "one")
    store.add("two", "two")
    sent = []
    runner, _ = make_runner(
        store, [FakeTurn(err_result("rate_limit_error")),
                FakeTurn(err_result("insufficient credits"))],
        sender=collect_sender(sent), concurrency=2, **budget_kw(tmp_path))

    runner.check_now()  # both claimed in one pass; both fail with pushback

    assert len(park_pings(sent)) == 1


def test_free_form_failure_text_with_loose_words_does_not_park(tmp_path):
    # "insufficient permissions" and "disk quota exceeded" are one job's
    # problem (file perms, a full disk), not the credit pool pushing back.
    from iris.budget import BudgetState

    store = JobStore(tmp_path / "jobs.json")
    store.add("one", "one")
    store.add("two", "two")
    sent = []
    runner, _ = make_runner(
        store, [FakeTurn(err_result("insufficient permissions to write the file")),
                FakeTurn(err_result("disk quota exceeded"))],
        sender=collect_sender(sent), concurrency=2, **budget_kw(tmp_path))

    runner.check_now()

    assert BudgetState(tmp_path / "budget.json").park_until == 0.0
    assert park_pings(sent) == []


def test_an_ordinary_failure_does_not_park(tmp_path):
    from iris.budget import BudgetState

    store = JobStore(tmp_path / "jobs.json")
    store.add("doomed", "doomed")
    sent = []
    runner, _ = make_runner(store, [FakeTurn(err_result("the worker crashed"))],
                            sender=collect_sender(sent), **budget_kw(tmp_path))

    runner.check_now()

    assert BudgetState(tmp_path / "budget.json").park_until == 0.0
    assert park_pings(sent) == []


def test_a_cancel_with_pushback_text_does_not_park(tmp_path):
    # The owner killed the job; the flag wins, so its free-form error text
    # (which may mention credit) must not park the fleet.
    from iris.budget import BudgetState

    store = JobStore(tmp_path / "jobs.json")
    jid = store.add("long haul", "long")
    turn = FakeTurn(err_result("Credit balance is too low"), hold=True, landed=True)
    sent = []
    runner, _ = make_runner(store, [turn], sync=False,
                            sender=collect_sender(sent), **budget_kw(tmp_path))

    runner.check_now()
    assert runner.turn_registered.wait(timeout=2)
    store.request_cancel(jid)
    runner.check_now()
    runner.workers[jid].join(timeout=2)

    assert store.get(jid)["status"] == "cancelled"
    assert BudgetState(tmp_path / "budget.json").park_until == 0.0
    assert park_pings(sent) == []


def test_without_a_budget_state_path_pushback_changes_nothing(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("doomed", "doomed")
    later = store.add("later", "later")
    sent = []
    runner, sd = make_runner(store, [FakeTurn(err_result("Credit balance is too low"))],
                             sender=collect_sender(sent), concurrency=1)

    runner.check_now()
    sd.turns.append(FakeTurn(ok_result()))
    runner.check_now()  # no guard configured: claiming continues

    assert store.get(later)["status"] == "done"
    assert park_pings(sent) == []


class FakeClock:
    """Settable wall clock for the credit-guard paths (runner clock seam)."""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def test_watcher_notices_park_expiry_without_a_registry_change(tmp_path):
    # An idle bot: nothing writes the registry while parked, so mtime never
    # moves. The watcher itself must notice the park deadline passing and
    # claim the queued job.
    import time as _time

    from iris.budget import BudgetState

    store = JobStore(tmp_path / "jobs.json")
    jid = store.add("queued under the park", "queued")
    clock = FakeClock(_time.time())
    state_path = tmp_path / "budget.json"
    BudgetState(state_path).set_park_until(clock.now + 3600)
    sent = []
    runner, _ = make_runner(store, [FakeTurn(ok_result())], sync=False,
                            poll_seconds=0.01, clock=clock,
                            sender=collect_sender(sent),
                            budget_state_path=str(state_path), park_minutes=60.0)
    first_check = threading.Event()
    real_check = runner.check_now

    def wrapped_check():
        real_check()
        first_check.set()

    runner.check_now = wrapped_check
    runner.start()
    try:
        # First sighting of the registry file: checked, but parked, no claim.
        assert first_check.wait(timeout=2)
        assert store.get(jid)["status"] == "pending"

        clock.now += 3700  # past expiry; the registry is NOT touched
        assert runner.turn_registered.wait(timeout=2)
        runner.workers[jid].join(timeout=2)
    finally:
        runner.stop()

    assert store.get(jid)["status"] == "done"
    assert len([t for _, t, _ in sent if t.startswith("jobs resumed")]) == 1
    assert BudgetState(state_path).park_until == 0.0


def test_the_resumed_ping_is_sent_after_the_check_lock_is_released(tmp_path):
    # The resume ping is Discord I/O (up to ~20s); holding _check_lock through
    # it would stall every other cancel/claim pass for the duration.
    import time as _time

    from iris.budget import BudgetState

    store = JobStore(tmp_path / "jobs.json")
    state_path = tmp_path / "budget.json"
    BudgetState(state_path).set_park_until(_time.time() - 1)
    sent, lock_free = [], []
    runner, _ = make_runner(store, [], budget_state_path=str(state_path),
                            park_minutes=60.0)

    def probing_sender(channel, text, token):
        if text.startswith("jobs resumed"):
            acquired = runner._check_lock.acquire(blocking=False)
            lock_free.append(acquired)
            if acquired:
                runner._check_lock.release()
        sent.append((channel, text, token))
        return True

    runner.sender = probing_sender
    runner.check_now()

    assert [t for _, t, _ in sent if t.startswith("jobs resumed")] != []
    assert lock_free == [True]  # the lock was already released at send time
    assert BudgetState(state_path).park_until == 0.0


def test_watch_pass_nudges_when_the_tick_cleared_the_park_first(tmp_path):
    # The reminders tick clears the park in the shared state file without ever
    # touching the registry; the next watcher pass must claim (and the runner
    # owes no resume ping: the tick, having cleared, already pinged).
    import time as _time

    from iris.budget import BudgetState

    store = JobStore(tmp_path / "jobs.json")
    jid = store.add("queued under the park", "queued")
    clock = FakeClock(_time.time())
    state_path = tmp_path / "budget.json"
    BudgetState(state_path).set_park_until(clock.now + 3600)
    sent = []
    runner, _ = make_runner(store, [FakeTurn(ok_result())], clock=clock,
                            sender=collect_sender(sent),
                            budget_state_path=str(state_path), park_minutes=60.0)

    runner._watch_pass()  # first sighting: checked, parked, no claim
    assert store.get(jid)["status"] == "pending"
    runner._watch_pass()  # nothing changed: still parked
    assert store.get(jid)["status"] == "pending"

    BudgetState(state_path).set_park_until(0.0)  # the tick cleared it
    runner._watch_pass()

    assert store.get(jid)["status"] == "done"
    assert [t for _, t, _ in sent if t.startswith("jobs resumed")] == []


# -- credit guard: near-cap tightening --------------------------------------------


def write_month_spend(tmp_path, cost):
    import json
    import time as _time

    path = tmp_path / "metrics.jsonl"
    path.write_text(json.dumps({"ts": _time.time(), "conversation_id": "discord:1",
                                "cost_usd": cost}) + "\n", encoding="utf-8")
    return path


def test_tightening_routes_unpinned_jobs_to_the_light_model(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("work", "title")  # no model pin
    metrics = write_month_spend(tmp_path, 80.0)  # exactly 80%: tight
    runner, sd = make_runner(store, [FakeTurn(ok_result())], sender=collect_sender([]),
                             metrics_path=str(metrics), monthly_credit=100.0,
                             light_model="claude-haiku-4-5")

    runner.check_now()

    assert sd.calls == [("work", None, "claude-haiku-4-5")]


def test_tightening_always_honors_a_pinned_model(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("work", "title", model="claude-opus-4-6")
    metrics = write_month_spend(tmp_path, 95.0)
    runner, sd = make_runner(store, [FakeTurn(ok_result())], sender=collect_sender([]),
                             metrics_path=str(metrics), monthly_credit=100.0,
                             light_model="claude-haiku-4-5")

    runner.check_now()

    assert sd.calls == [("work", None, "claude-opus-4-6")]


def test_no_light_model_configured_means_no_tightening(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("work", "title")
    metrics = write_month_spend(tmp_path, 95.0)
    runner, sd = make_runner(store, [FakeTurn(ok_result())], sender=collect_sender([]),
                             metrics_path=str(metrics), monthly_credit=100.0)

    runner.check_now()

    assert sd.calls == [("work", None, None)]


def test_below_eighty_percent_keeps_the_default_model(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("work", "title")
    metrics = write_month_spend(tmp_path, 79.99)
    runner, sd = make_runner(store, [FakeTurn(ok_result())], sender=collect_sender([]),
                             metrics_path=str(metrics), monthly_credit=100.0,
                             light_model="claude-haiku-4-5")

    runner.check_now()

    assert sd.calls == [("work", None, None)]


def test_no_monthly_credit_means_no_tightening(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("work", "title")
    metrics = write_month_spend(tmp_path, 9999.0)
    runner, sd = make_runner(store, [FakeTurn(ok_result())], sender=collect_sender([]),
                             metrics_path=str(metrics), light_model="claude-haiku-4-5")

    runner.check_now()

    assert sd.calls == [("work", None, None)]


# -- start / recovery / watcher -----------------------------------------------


def test_start_flips_orphaned_running_jobs_to_interrupted_and_pings(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    jid = store.add("was running", "orphan")
    store.claim_pending(1)  # running on disk, but no live handle in this runner
    sent = []

    def exploding_factory():
        raise AssertionError("recovery must not spend a model call")

    runner, _ = make_runner(store, [], sender=collect_sender(sent),
                            notify_driver_factory=exploding_factory)
    runner.start()
    runner.stop()

    assert store.get(jid)["status"] == "interrupted"
    assert len(sent) == 1
    channel, text, token = sent[0]
    assert channel == "999" and token == "tok"
    assert "orphan" in text  # forced spine ping names the job


def test_start_in_sync_mode_skips_the_watcher_thread(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    runner, _ = make_runner(store, [], sender=collect_sender([]))  # sync=True

    runner.start()

    assert runner.watcher is None
    runner.stop()


def test_watcher_calls_check_now_when_the_store_mtime_changes(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    runner, _ = make_runner(store, [], sync=False, poll_seconds=0.01,
                            sender=collect_sender([]))
    checked = threading.Event()
    runner.check_now = checked.set  # observe the nudge, run nothing

    runner.start()
    try:
        store.add("new work", "new")  # touches the registry file
        assert checked.wait(timeout=2)
    finally:
        runner.stop()

    assert runner.watcher is not None
    assert not runner.watcher.is_alive()  # stop() joined the watcher


# -- stamping ------------------------------------------------------------------


def test_job_created_inside_one_turn_window_is_stamped_and_picked_up(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    runner, _ = make_runner(store, [FakeTurn(ok_result())], sender=collect_sender([]))

    runner.turn_started("discord:7", "7")
    jid = store.add("spawned mid-turn", "midturn")
    runner.turn_finished("discord:7")

    job = store.get(jid)
    assert job["conversation_id"] == "discord:7"
    assert job["channel_id"] == "7"
    # turn_finished nudged check_now: in sync mode the job already ran.
    assert job["status"] == "done"


def test_job_created_during_overlapping_windows_stays_unstamped(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    runner, _ = make_runner(store, [FakeTurn(ok_result())], sender=collect_sender([]))

    runner.turn_started("discord:1", "1")
    runner.turn_started("discord:2", "2")
    jid = store.add("ambiguous", "ambiguous")

    runner.turn_finished("discord:1")
    assert store.get(jid)["conversation_id"] == ""
    # The ambiguity is remembered: the surviving window must not adopt the job
    # once the competing one is gone.
    runner.turn_finished("discord:2")
    assert store.get(jid)["conversation_id"] == ""
    assert store.get(jid)["channel_id"] == ""


def test_job_created_before_the_window_opened_is_not_stamped(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    jid = store.add("early bird", "early")
    runner, _ = make_runner(store, [FakeTurn(ok_result())], sender=collect_sender([]))

    runner.turn_started("discord:7", "7")
    runner.turn_finished("discord:7")

    assert store.get(jid)["conversation_id"] == ""


# -- concurrency / modes / metrics ---------------------------------------------


def test_concurrency_cap_keeps_the_second_job_pending(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    first = store.add("slow one", "slow")
    second = store.add("next up", "next")
    slow = FakeTurn(ok_result(text="first"), hold=True)
    fast = FakeTurn(ok_result(text="second"))
    runner, _ = make_runner(store, [slow, fast], sync=False, concurrency=1,
                            sender=collect_sender([]))

    runner.check_now()
    assert runner.turn_registered.wait(timeout=2)
    # One slot, one job in flight; repeated checks must not start a second.
    runner.check_now()
    runner.check_now()
    assert [j["id"] for j in store.all(status="running")] == [first]
    assert store.get(second)["status"] == "pending"

    slow.release()
    runner.workers[first].join(timeout=2)
    assert not runner.workers[first].is_alive()

    runner.check_now()  # the freed slot picks up the queued job
    runner.workers[second].join(timeout=2)
    assert store.get(first)["status"] == "done"
    assert store.get(second)["status"] == "done"


def test_sync_mode_runs_workers_inline_without_threads(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    jid = store.add("inline", "inline")
    runner, _ = make_runner(store, [FakeTurn(ok_result())], sender=collect_sender([]))

    runner.check_now()

    assert runner.workers == {}  # no thread was spawned
    assert store.get(jid)["status"] == "done"


def test_each_finished_job_emits_one_metrics_record(tmp_path):
    import json

    store = JobStore(tmp_path / "jobs.json")
    jid = store.add("measured", "measured")
    metrics = tmp_path / "metrics.jsonl"
    runner, _ = make_runner(store, [FakeTurn(ok_result())],
                            sender=collect_sender([]),
                            metrics_path=str(metrics))

    runner.check_now()

    lines = metrics.read_text("utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["conversation_id"] == f"job:{jid}"
    assert record["cost_usd"] == 0.01
    assert record["is_error"] is False
    assert record["turns"] == 1


def test_a_stamp_landing_mid_run_is_honored_at_delivery_time(tmp_path):
    # The worker holds the claimed snapshot; the record is re-read at delivery
    # so a turn_finished stamp that arrived while the job ran still folds back.
    store = JobStore(tmp_path / "jobs.json")
    jid = store.add("late stamp", "late")
    turn = FakeTurn(ok_result(text="report"), hold=True)
    delivered, sent = [], []
    runner, _ = make_runner(
        store, [turn], sync=False,
        deliver=lambda ch, cid, text: delivered.append((ch, cid, text)) or True,
        sender=collect_sender(sent),
    )

    runner.check_now()
    assert runner.turn_registered.wait(timeout=2)
    store.update(jid, conversation_id="discord:8", channel_id="8")
    turn.release()
    runner.workers[jid].join(timeout=2)

    assert delivered == [
        ("8", "discord:8", f'[background job #{jid} "late" finished]\nreport')
    ]
    assert sent == []


def test_a_deliver_callback_that_raises_falls_back_to_the_spine(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("work", "title", channel_id="42", conversation_id="discord:42")
    sent = []

    def deliver(channel_id, conversation_id, text):
        raise RuntimeError("stale runner")

    runner, _ = make_runner(store, [FakeTurn(ok_result())], deliver=deliver,
                            sender=collect_sender(sent))
    runner.check_now()

    assert len(sent) == 1
    assert sent[0][0] == "42"
    assert sent[0][1].startswith("job done: title in")


def test_dead_workers_are_pruned_on_the_next_check(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.add("quick work", "quick")
    runner, _ = make_runner(store, [FakeTurn(ok_result())],
                            sender=collect_sender([]), sync=False)

    runner.check_now()
    for thread in list(runner.workers.values()):
        thread.join(2)
    assert runner.workers  # finished but not yet pruned

    runner.check_now()
    assert runner.workers == {}
