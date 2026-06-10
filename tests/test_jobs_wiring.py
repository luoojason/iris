"""Wiring tests: JobRunner.from_config mapping, the Discord fold-back deliver
closure, and the per-turn stamping brackets.

build_client itself needs the discord SDK, so (like should_handle before it)
the testable seams are module-level: make_job_deliver and the bracket wrappers
take their loop/resolvers/runner as plain callables and fakes here. No
conftest; no real claude, network, or Discord.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from iris.config import Config
from iris.conversation import Turn
from iris.discord_adapter import _BracketedLiveHandle, bracket_run_turn, make_job_deliver
from iris.driver import ClaudeDriver
from iris.jobs import JobRunner


# -- JobRunner.from_config ----------------------------------------------------


def jobs_config(tmp_path, **overrides) -> Config:
    fields = dict(
        jobs_file=str(tmp_path / "jobs.json"),
        job_concurrency=3,
        job_idle_timeout=120.0,
        job_poll_seconds=1.5,
        job_model="",
        job_grants=["Task", "Bash"],
        notify_channel="999",
        discord_token="tok",
        watch_min_seconds=10.0,
        metrics_file=str(tmp_path / "metrics.jsonl"),
        claude_bin="claude-test-bin",
        model="claude-sonnet-4-6",
        notify_persona="notify-persona.md",
    )
    fields.update(overrides)
    return Config(**fields)


def test_from_config_maps_every_field(tmp_path):
    config = jobs_config(tmp_path)
    base = ClaudeDriver(model="claude-sonnet-4-6")

    runner = JobRunner.from_config(config, base)

    assert str(runner.store.path) == str(tmp_path / "jobs.json")
    assert runner.base_driver is base  # no job model: the chat driver as-is
    assert runner.grant_ceiling == ("Task", "Bash")
    assert runner.concurrency == 3
    assert runner.idle_timeout == 120.0
    assert runner.poll_seconds == 1.5
    assert runner.notify_channel == "999"
    assert runner.discord_token == "tok"
    assert runner.watch_min_seconds == 10.0
    assert runner.metrics_path == str(tmp_path / "metrics.jsonl")


def test_from_config_overrides_the_model_when_job_model_is_set(tmp_path):
    config = jobs_config(tmp_path, job_model="claude-haiku-4-5")
    base = ClaudeDriver(model="claude-sonnet-4-6", timeout=77.0)

    runner = JobRunner.from_config(config, base)

    assert runner.base_driver is not base       # replaced copy, never mutated
    assert runner.base_driver.model == "claude-haiku-4-5"
    assert runner.base_driver.timeout == 77.0   # everything else carries over
    assert base.model == "claude-sonnet-4-6"


def test_from_config_notify_driver_factory_builds_the_triage_one_shot(tmp_path):
    config = jobs_config(tmp_path)
    runner = JobRunner.from_config(config, ClaudeDriver())

    driver = runner.notify_driver_factory()

    # build_notify_driver's shape: notify persona, short timeout, no retries.
    assert driver.claude_bin == "claude-test-bin"
    assert driver.append_system_prompt_file == "notify-persona.md"
    assert driver.timeout == 60
    assert driver.max_retries == 0


def test_from_config_passes_deliver_and_sender_through(tmp_path):
    config = jobs_config(tmp_path)
    deliver = lambda *a: True  # noqa: E731
    sender = lambda *a: True   # noqa: E731

    runner = JobRunner.from_config(config, ClaudeDriver(), deliver=deliver, sender=sender)

    assert runner.deliver is deliver
    assert runner.sender is sender


def test_from_config_wires_the_workspace_store_and_attachments_dir(tmp_path):
    # The runner is the resolution point for workspace NAMES and the boundary
    # owner for artifact uploads, so both knobs must arrive from Config.
    config = jobs_config(tmp_path, workspaces_file=str(tmp_path / "ws.json"),
                         attachments_dir=str(tmp_path / "attach"))

    runner = JobRunner.from_config(config, ClaudeDriver())

    assert str(runner.workspace_store.path) == str(tmp_path / "ws.json")
    assert runner.attachments_dir == str(tmp_path / "attach")
    assert runner.uploader is None  # production: reminders.send_discord_file


# -- make_job_deliver ---------------------------------------------------------


class RecordingRunner:
    """Quacks like a ConversationRunner: records submitted turns."""

    def __init__(self):
        self.turns: list[Turn] = []

    def submit(self, turn: Turn) -> None:
        self.turns.append(turn)


@pytest.fixture()
def loop_in_thread():
    """A running event loop on a background thread, like the Discord client's."""
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield loop
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def make_resolvers(channels, runners):
    async def resolve_channel(channel_id):
        return channels.get(channel_id)

    def resolve_runner(conversation_id, channel):
        return runners.get(conversation_id)

    return resolve_channel, resolve_runner


def test_deliver_before_the_loop_exists_is_false():
    # Before on_ready there is no loop to marshal onto; the runner must fall
    # back to the notify spine, so the closure reports failure cleanly.
    calls = []

    async def resolve_channel(channel_id):
        calls.append(channel_id)

    deliver = make_job_deliver(lambda: None, resolve_channel, lambda *a: None)

    assert deliver("42", "discord:42", "report") is False
    assert calls == []  # nothing was resolved without a loop


def test_deliver_happy_path_submits_a_turn_to_the_resolved_runner(loop_in_thread):
    runner = RecordingRunner()
    resolve_channel, resolve_runner = make_resolvers(
        {"42": object()}, {"discord:42": runner})
    deliver = make_job_deliver(lambda: loop_in_thread, resolve_channel, resolve_runner)

    assert deliver("42", "discord:42", "the findings") is True

    assert len(runner.turns) == 1
    assert runner.turns[0].text == "the findings"


def test_deliver_resolves_the_runner_at_delivery_time(loop_in_thread):
    # A !reset pops the runner from the adapter's dict; the next delivery must
    # reach whatever runner the dict holds NOW, never a captured stale one.
    first, second = RecordingRunner(), RecordingRunner()
    runners = {"discord:42": first}
    resolve_channel, resolve_runner = make_resolvers({"42": object()}, runners)
    deliver = make_job_deliver(lambda: loop_in_thread, resolve_channel, resolve_runner)

    assert deliver("42", "discord:42", "one") is True
    runners["discord:42"] = second  # the reset-and-recreate
    assert deliver("42", "discord:42", "two") is True

    assert [t.text for t in first.turns] == ["one"]
    assert [t.text for t in second.turns] == ["two"]


def test_deliver_unresolvable_channel_is_false(loop_in_thread):
    resolve_channel, resolve_runner = make_resolvers({}, {"discord:42": RecordingRunner()})
    deliver = make_job_deliver(lambda: loop_in_thread, resolve_channel, resolve_runner)

    assert deliver("42", "discord:42", "report") is False


def test_deliver_unresolvable_runner_is_false(loop_in_thread):
    resolve_channel, resolve_runner = make_resolvers({"42": object()}, {})
    deliver = make_job_deliver(lambda: loop_in_thread, resolve_channel, resolve_runner)

    assert deliver("42", "discord:42", "report") is False


def test_deliver_swallows_exceptions_into_false(loop_in_thread):
    async def resolve_channel(channel_id):
        raise RuntimeError("discord hiccup")

    deliver = make_job_deliver(lambda: loop_in_thread, resolve_channel, lambda *a: None)

    assert deliver("42", "discord:42", "report") is False


def test_a_timed_out_delivery_never_submits_a_late_turn(loop_in_thread):
    # The give-up must not abandon a live coroutine: when channel resolution
    # stalls past the timeout and then succeeds, a late runner.submit would
    # deliver the job twice (spine ping AND fold-back turn), breaking the
    # never-both rule and spending a second model call on a failed job.
    runner = RecordingRunner()
    resolved = threading.Event()

    async def slow_resolve(channel_id):
        await asyncio.sleep(0.2)
        resolved.set()
        return object()

    deliver = make_job_deliver(lambda: loop_in_thread, slow_resolve,
                               lambda cid, ch: runner, timeout=0.05, grace=2.0)

    assert deliver("42", "discord:42", "report") is False
    assert resolved.is_set()  # the stalled resolve did complete...
    assert runner.turns == []  # ...but the abandoned submit was suppressed


def test_a_delivery_hung_past_the_grace_window_is_cancelled(loop_in_thread):
    # A resolve that never returns within timeout + grace is cancelled
    # outright; the spine fallback owns the job from here.
    runner = RecordingRunner()

    async def hung_resolve(channel_id):
        await asyncio.sleep(60)
        return object()

    deliver = make_job_deliver(lambda: loop_in_thread, hung_resolve,
                               lambda cid, ch: runner, timeout=0.05, grace=0.05)

    assert deliver("42", "discord:42", "report") is False
    assert runner.turns == []


# -- turn bracketing ----------------------------------------------------------


class RecordingJobRunner:
    """Quacks like JobRunner's stamping surface: records the window calls."""

    def __init__(self):
        self.started: list[tuple[str, str]] = []
        self.finished: list[str] = []

    def turn_started(self, conversation_id, channel_id=""):
        self.started.append((conversation_id, channel_id))

    def turn_finished(self, conversation_id):
        self.finished.append(conversation_id)


def test_bracket_run_turn_opens_and_closes_the_window_around_the_turn():
    job_runner = RecordingJobRunner()
    order = []

    async def run_turn(prompt, has_attachments):
        order.append(("turn", list(job_runner.started), list(job_runner.finished)))
        return "reply"

    wrapped = bracket_run_turn(run_turn, job_runner, "discord:7", "7")
    reply = asyncio.run(wrapped("hello", False))

    assert reply == "reply"
    # The window was already open while the turn ran, and not yet closed.
    assert order == [("turn", [("discord:7", "7")], [])]
    assert job_runner.finished == ["discord:7"]


def test_bracket_run_turn_closes_the_window_even_when_the_turn_raises():
    job_runner = RecordingJobRunner()

    async def run_turn(prompt, has_attachments):
        raise RuntimeError("turn exploded")

    wrapped = bracket_run_turn(run_turn, job_runner, "discord:7", "7")
    with pytest.raises(RuntimeError):
        asyncio.run(wrapped("hello", False))

    assert job_runner.started == [("discord:7", "7")]
    assert job_runner.finished == ["discord:7"]


def test_bracket_run_turn_without_a_job_runner_returns_the_turn_unwrapped():
    async def run_turn(prompt, has_attachments):
        return "reply"

    assert bracket_run_turn(run_turn, None, "discord:7", "7") is run_turn


class FakeLiveHandle:
    """A minimal LiveHandle: scripted begin/result, idempotent close."""

    def __init__(self, begin_error=None):
        self.begin_error = begin_error
        self.closed = 0

    async def begin(self):
        if self.begin_error is not None:
            raise self.begin_error

    def is_open(self):
        return True

    async def inject(self, text):
        return True

    async def result(self):
        return "reply"

    async def aftermath(self):
        return []

    def close(self):
        self.closed += 1


def test_bracketed_live_handle_brackets_begin_to_close():
    job_runner = RecordingJobRunner()
    inner = FakeLiveHandle()
    handle = _BracketedLiveHandle(inner, job_runner, "discord:9", "9")

    async def drive():
        await handle.begin()
        assert job_runner.started == [("discord:9", "9")]
        assert job_runner.finished == []  # window spans the whole turn
        assert await handle.result() == "reply"
        assert await handle.inject("more") is True
        assert handle.is_open() is True
        assert await handle.aftermath() == []
        handle.close()

    asyncio.run(drive())
    assert job_runner.finished == ["discord:9"]
    assert inner.closed == 1


def test_bracketed_live_handle_close_is_idempotent_about_the_window():
    job_runner = RecordingJobRunner()
    handle = _BracketedLiveHandle(FakeLiveHandle(), job_runner, "discord:9", "9")

    async def drive():
        await handle.begin()
        handle.close()
        handle.close()  # the underlying close is idempotent; the window too

    asyncio.run(drive())
    assert job_runner.finished == ["discord:9"]


def test_bracketed_live_handle_failed_begin_still_closes_the_window():
    # LiveConversationRunner calls close() when begin() raises, so the window
    # opened by begin must be closed there, never leaked.
    job_runner = RecordingJobRunner()
    handle = _BracketedLiveHandle(
        FakeLiveHandle(begin_error=RuntimeError("no session")),
        job_runner, "discord:9", "9")

    async def drive():
        with pytest.raises(RuntimeError):
            await handle.begin()
        handle.close()

    asyncio.run(drive())
    assert job_runner.started == [("discord:9", "9")]
    assert job_runner.finished == ["discord:9"]


def test_close_without_begin_never_fires_turn_finished():
    job_runner = RecordingJobRunner()
    handle = _BracketedLiveHandle(FakeLiveHandle(), job_runner, "discord:9", "9")

    handle.close()

    assert job_runner.started == []
    assert job_runner.finished == []


class ExplodingJobRunner(RecordingJobRunner):
    """turn_finished raises, like a registry write hitting a full disk."""

    def turn_finished(self, conversation_id):
        super().turn_finished(conversation_id)
        raise OSError("registry write failed")


def test_bracketed_close_still_closes_the_handle_when_turn_finished_raises():
    # close() MUST run on every exit path (conversation.py relies on it to
    # release the per-conversation Agent lock); a job-registry I/O failure in
    # the window close can never be allowed to leak the lock.
    job_runner = ExplodingJobRunner()
    inner = FakeLiveHandle()
    handle = _BracketedLiveHandle(inner, job_runner, "discord:9", "9")

    async def drive():
        await handle.begin()
        handle.close()  # must not raise, and must close the inner handle

    asyncio.run(drive())
    assert inner.closed == 1
    assert job_runner.finished == ["discord:9"]
