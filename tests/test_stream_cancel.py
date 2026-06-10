"""Public StreamTurn.cancel() tests against a scripted fake process.

cancel() is the jobs system's kill switch: it must go through the same
hardened group-kill the watchdog uses, release every waiter promptly, hand
pre-primary waiters an is_error "cancelled" result, and leave a landed reply
untouched. Fakes copied from tests/test_stream_driver.py (no conftest).
"""

from __future__ import annotations

import json
import queue
import threading

from iris.stream_driver import StreamTurn


_EOF = object()


class _LineStream:
    """A blocking line iterator the test feeds, like a subprocess pipe."""

    def __init__(self) -> None:
        self._q: "queue.Queue" = queue.Queue()

    def push(self, line: str) -> None:
        self._q.put(line)

    def eof(self) -> None:
        self._q.put(_EOF)

    def __iter__(self):
        return self

    def __next__(self):
        item = self._q.get()
        if item is _EOF:
            raise StopIteration
        return item


class _Stdin:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.closed = False

    def write(self, s: str) -> None:
        if self.closed:
            raise ValueError("I/O operation on closed file")
        self.lines.append(s)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = _Stdin()
        self.stdout = _LineStream()
        self.stderr = _LineStream()
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.stdout.eof()
        self.stderr.eof()

    def wait(self, timeout=None):
        return 0


def _init(sid="sess-1"):
    return json.dumps({"type": "system", "subtype": "init", "session_id": sid}) + "\n"


def _ok(text, sid="sess-1"):
    return json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": text, "session_id": sid,
    }) + "\n"


def _err(sid="sess-1"):
    return json.dumps({
        "type": "result", "subtype": "error_during_execution", "is_error": True,
        "session_id": sid,
    }) + "\n"


def _finish(proc: FakeProcess) -> None:
    proc.stderr.eof()
    proc.stdout.eof()


def test_cancel_kills_a_live_turn_and_returns_true():
    proc = FakeProcess()
    turn = StreamTurn(proc).start("long research job")
    proc.stdout.push(_init())

    assert turn.open is True
    assert turn.cancel() is True
    assert turn.wait_finished(timeout=2)
    assert proc.killed is True
    assert turn.open is False


def test_cancel_before_primary_yields_a_cancelled_error_result():
    proc = FakeProcess()
    turn = StreamTurn(proc).start("work that never replies")
    proc.stdout.push(_init())

    assert turn.cancel() is True
    res = turn.wait_primary(timeout=2)
    assert turn.wait_finished(timeout=2)
    assert res is not None and res.is_error is True
    assert "cancelled" in (res.error or "")


def test_double_cancel_is_idempotent_and_second_call_returns_false():
    proc = FakeProcess()
    turn = StreamTurn(proc).start("work")
    proc.stdout.push(_init())

    assert turn.cancel() is True
    # Immediately again, before finalize has necessarily run, and once more
    # after the turn is fully down: both must be no-ops reporting False.
    assert turn.cancel() is False
    assert turn.wait_finished(timeout=2)
    assert turn.cancel() is False


def test_cancel_after_turn_finished_returns_false_and_keeps_the_result():
    proc = FakeProcess()
    turn = StreamTurn(proc).start("quick one")
    proc.stdout.push(_init())
    proc.stdout.push(_ok("done"))
    _finish(proc)
    assert turn.wait_finished(timeout=2)

    assert turn.cancel() is False
    assert proc.killed is False
    res = turn.wait_primary(timeout=2)
    assert res is not None and res.text == "done"
    assert res.is_error is False


def test_cancel_releases_blocked_waiters_promptly():
    proc = FakeProcess()
    turn = StreamTurn(proc).start("work")
    proc.stdout.push(_init())

    boxes: dict = {}

    def wait_for_primary():
        boxes["primary"] = turn.wait_primary(timeout=10)

    def wait_for_finished():
        boxes["finished"] = turn.wait_finished(timeout=10)

    t1 = threading.Thread(target=wait_for_primary, daemon=True)
    t2 = threading.Thread(target=wait_for_finished, daemon=True)
    t1.start()
    t2.start()

    assert turn.cancel() is True
    t1.join(timeout=2)
    t2.join(timeout=2)
    assert not t1.is_alive() and not t2.is_alive()
    assert boxes["finished"] is True
    assert boxes["primary"] is not None and boxes["primary"].is_error is True


def test_cancel_after_primary_landed_keeps_the_reply_and_reaps_the_process():
    proc = FakeProcess()
    turn = StreamTurn(proc).start("task")
    proc.stdout.push(_init())
    proc.stdout.push(_ok("the answer"))
    res = turn.wait_primary(timeout=2)
    assert res is not None and res.text == "the answer"

    # Streams still open (a stray could be in flight), so the turn is live:
    # cancel kills the leftover process but must not disturb the stored reply.
    assert turn.cancel() is True
    assert turn.wait_finished(timeout=2)
    assert proc.killed is True
    final = turn.wait_primary(timeout=2)
    assert final is not None and final.text == "the answer"
    assert final.is_error is False


def test_cancel_keeps_the_deferred_stderr_fold_for_a_landed_error():
    proc = FakeProcess()
    turn = StreamTurn(proc).start("hi")
    proc.stdout.push(_init())
    # An error result lands (deferred until stderr closes), then cancel hits:
    # the real cause from stderr must still win over a generic cancel message.
    proc.stderr.push("No conversation found with session ID: 00000000-dead\n")
    proc.stdout.push(_err())

    assert turn.cancel() is True
    res = turn.wait_primary(timeout=2)
    assert turn.wait_finished(timeout=2)
    assert res is not None and res.is_error is True
    assert "No conversation found" in (res.error or "")
