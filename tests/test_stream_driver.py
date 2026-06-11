"""StreamTurn lifecycle tests against a scripted fake process.

The real millisecond boundary race cannot be unit-tested, but it reduces to
three observable branches, which can: an injection while the turn is open folds
into it, an injection after it closes is refused, and a message that started a
second turn is captured as a stray. Plus the two empirical gotchas the probes
surfaced: stderr-only failures (dead session) and a hung process.
"""

from __future__ import annotations

import json
import queue
import threading

import pytest

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

    # -- test helpers ------------------------------------------------------

    def injected(self) -> list[str]:
        """The text of every user message written to stdin (parsed back)."""
        out = []
        for raw in self.stdin.lines:
            msg = json.loads(raw)
            out.append(msg["message"]["content"])
        return out


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


def test_inject_while_open_folds_into_one_turn():
    proc = FakeProcess()
    turn = StreamTurn(proc).start("do the thing")
    proc.stdout.push(_init())

    assert turn.open is True
    assert turn.inject("also mention Y") is True
    assert "also mention Y" in proc.injected()

    proc.stdout.push(_ok("did it, with Y"))
    _finish(proc)

    res = turn.wait_primary(timeout=2)
    assert turn.wait_finished(timeout=2)
    assert res is not None and res.text == "did it, with Y"
    assert res.is_error is False
    assert turn.strays == []          # one turn, one result
    assert turn.open is False


def test_inject_after_close_is_refused():
    proc = FakeProcess()
    turn = StreamTurn(proc).start("quick one")
    proc.stdout.push(_init())
    proc.stdout.push(_ok("done"))

    assert turn.wait_primary(timeout=2) is not None   # primary seen -> turn closed
    assert turn.open is False
    assert turn.inject("too late") is False           # caller must re-queue this
    assert "too late" not in proc.injected()

    _finish(proc)
    assert turn.wait_finished(timeout=2)


def test_raced_message_is_captured_as_stray():
    proc = FakeProcess()
    turn = StreamTurn(proc).start("first task")
    proc.stdout.push(_init())

    # Message written while open (accepted), but it lands at the boundary and
    # claude runs it as a second turn: two results come back.
    assert turn.inject("raced message") is True
    proc.stdout.push(_ok("primary answer"))
    proc.stdout.push(_ok("stray answer"))
    _finish(proc)

    res = turn.wait_primary(timeout=2)
    assert turn.wait_finished(timeout=2)
    assert res.text == "primary answer"
    strays = turn.strays
    assert len(strays) == 1 and strays[0].text == "stray answer"


def test_dead_session_error_folds_stderr():
    proc = FakeProcess()
    turn = StreamTurn(proc).start("hi")
    proc.stdout.push(_init())
    # Dead --resume: empty error on the result event, real cause on stderr.
    proc.stderr.push("No conversation found with session ID: 00000000-dead\n")
    proc.stderr.eof()
    proc.stdout.push(_err())
    proc.stdout.eof()

    res = turn.wait_primary(timeout=2)
    assert turn.wait_finished(timeout=2)
    assert res.is_error is True
    assert "No conversation found" in (res.error or "")


def test_watchdog_kills_a_hung_turn():
    proc = FakeProcess()
    turn = StreamTurn(proc, idle_timeout=0.2).start("work")
    proc.stdout.push(_init())
    # ...then silence. The watchdog should kill it.

    res = turn.wait_primary(timeout=3)
    assert turn.wait_finished(timeout=3)
    assert proc.killed is True
    assert res is not None and res.is_error is True
    assert "without a result" in (res.error or "")


def test_total_cap_kills_even_without_an_idle_hang():
    # Idle ceiling far away (5s) so only the total ceiling (0.25s) can fire: proves
    # the backstop is independent of silence, the case a long-but-lively turn hits.
    proc = FakeProcess()
    turn = StreamTurn(proc, idle_timeout=5.0, total_timeout=0.25).start("work")
    proc.stdout.push(_init())

    res = turn.wait_primary(timeout=3)
    assert turn.wait_finished(timeout=3)
    assert proc.killed is True
    assert res is not None and res.is_error is True
