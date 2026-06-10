"""Run one turn as a live stream-json process you can talk into mid-flight.

The one-shot driver (:mod:`iris.driver`) runs a turn as a closed box: prompt in,
wait, reply out. That cannot do a live interrupt, because there is no way to hand
the model another message once the box is sealed. This driver opens the box.

``claude -p --input-format stream-json --output-format stream-json`` keeps stdin
open for the life of a turn. A user message written there while the turn runs is
picked up at the model's next step boundary and folds into the *same* turn: a
hard "stop, do X instead" redirects it, a soft "also mention Y" is absorbed into
the work in flight. The turn still ends with exactly one ``result`` event.

One :class:`StreamTurn` owns one process for one turn. It is deliberately not
reused across turns: born with an initial prompt, wound down at its first
``result``. Keeping turns on separate processes preserves the upstream session
machinery (compaction between turns, per-turn model routing) unchanged, and
sidesteps a long-lived idle process.

Three things this module gets right that the happy path hides:

* **The boundary race.** A message can arrive in the sliver between the model
  finishing the turn and the reader seeing the ``result``. Injecting then would
  start a *second* turn. Rather than drop that message, the process is allowed to
  run it out and its answer is captured as a "stray" follow-up, so nothing the
  user sent is lost.
* **Stderr-only failures.** A dead ``--resume`` session reports "No conversation
  found ..." on *stderr* while the ``result`` event carries an empty error. The
  full stderr is folded into the error at finalize (when both streams have
  closed), so the upstream dead-session/overflow retries can recognize it.
* **Hangs.** A persistent reader (``for line in proc.stdout``) blocks forever if
  the model wedges. An idle watchdog kills a process that has gone silent past a
  timeout, which trips EOF and finalizes a clean error. The timeout is on
  *silence*, not total wall time, so a legitimately long redirect is never cut.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Optional, Sequence

from .driver import ClaudeDriver, ClaudeError, ClaudeResult, _child_env, parse_result_event

log = logging.getLogger("iris.stream")


def _user_message(text: str) -> str:
    """One stream-json user message line, as claude's stdin expects it."""
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"


# A process-like object: stdin (write/flush/close), stdout/stderr (iterable of
# lines), kill(), wait(). The default is subprocess.Popen; tests inject a fake.
Process = "subprocess.Popen[str]"


def _default_spawn(cmd: Sequence[str], env: dict) -> "subprocess.Popen[str]":
    return subprocess.Popen(
        list(cmd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )


class StreamTurn:
    """One running turn, on its own process, that accepts mid-flight messages."""

    def __init__(
        self,
        proc,
        *,
        fallback_session_id: Optional[str] = None,
        idle_timeout: float = 300.0,
        total_timeout: float = 1800.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._proc = proc
        self._session_id = fallback_session_id
        self._idle_timeout = idle_timeout
        self._total_timeout = total_timeout
        self._clock = clock
        self._start = clock()

        # _lock guards stdin writes and the _closed flag together, so deciding
        # "is the turn still open?" and writing into it cannot interleave with
        # the reader flipping the turn closed.
        self._lock = threading.Lock()
        self._closed = False

        self._primary_done = threading.Event()  # set when the reply is sendable
        self._finished = threading.Event()       # set when the process is fully done
        self._primary: Optional[ClaudeResult] = None
        self._primary_error_obj: Optional[dict] = None  # deferred until stderr is in
        self._strays: list[ClaudeResult] = []
        self._stderr_parts: list[str] = []

        self._last_event = clock()
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, name="stream-reader", daemon=True)
        self._watchdog = threading.Thread(target=self._watch, name="stream-watchdog", daemon=True)
        self._stderr_thread = threading.Thread(target=self._drain_stderr, name="stream-stderr", daemon=True)

    # -- lifecycle ---------------------------------------------------------

    def start(self, prompt: str) -> "StreamTurn":
        """Send the opening prompt and begin reading the turn."""
        self._write(prompt)
        self._stderr_thread.start()
        self._reader.start()
        self._watchdog.start()
        return self

    def _write(self, text: str) -> None:
        stdin = self._proc.stdin
        stdin.write(_user_message(text))
        stdin.flush()

    def inject(self, text: str) -> bool:
        """Feed a message into the running turn. True if it was accepted.

        False means the turn had already closed (the caller should treat the
        message as the start of the next turn). The check and the write happen
        under one lock so a message is never written into a turn that the reader
        has just flipped closed.
        """
        if not text or not text.strip():
            return True  # nothing to inject; treat as accepted, send nothing
        with self._lock:
            if self._closed:
                return False
            try:
                self._write(text)
            except (BrokenPipeError, ValueError, OSError):
                return False
            return True

    @property
    def open(self) -> bool:
        with self._lock:
            return not self._closed

    # -- reader / watchdog -------------------------------------------------

    def _drain_stderr(self) -> None:
        try:
            stderr = self._proc.stderr
            if stderr is None:
                return
            for line in stderr:
                self._stderr_parts.append(line)
        except Exception:
            pass

    def _watch(self) -> None:
        # Two ceilings, both ending in a kill that trips stdout EOF so _read_loop
        # finalizes an error. The idle ceiling catches a turn gone silent (a hang)
        # and only applies before the reply lands, since a single long tool call
        # is legitimately silent. The total ceiling is the runaway backstop: it
        # holds even after the reply, so a turn that keeps emitting forever, or
        # one whose teardown never completes, cannot wedge the conversation lock.
        step = max(0.05, min(1.0, self._idle_timeout / 4))
        while not self._stop.wait(step):
            now = self._clock()
            if not self._primary_done.is_set() and now - self._last_event >= self._idle_timeout:
                log.warning("stream turn idle past %.0fs; killing", self._idle_timeout)
                self._kill()
                return
            if now - self._start >= self._total_timeout:
                log.warning("stream turn exceeded %.0fs total; killing", self._total_timeout)
                self._kill()
                return

    def _kill(self) -> None:
        try:
            self._proc.kill()
        except Exception:
            pass

    def _read_loop(self) -> None:
        try:
            for line in self._proc.stdout:
                self._last_event = self._clock()
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._handle(obj)
        finally:
            self._finalize()

    def _handle(self, obj: dict) -> None:
        kind = obj.get("type")
        if kind == "system" and obj.get("subtype") == "init":
            sid = obj.get("session_id")
            if sid:
                self._session_id = sid
            return
        if kind == "result":
            self._handle_result(obj)

    def _handle_result(self, obj: dict) -> None:
        sid = obj.get("session_id")
        if sid:
            self._session_id = sid

        if self._primary_done.is_set() or self._primary_error_obj is not None:
            # A later result means a message raced the boundary and started a
            # second turn. Keep its answer as a stray follow-up.
            self._strays.append(parse_result_event(obj, self._session_id))
            return

        # First result: this turn is over. Refuse further injection immediately;
        # a message already written before this lands as a stray above.
        with self._lock:
            self._closed = True

        res = parse_result_event(obj, self._session_id)
        if res.is_error:
            # Defer: stderr (which may carry the real cause) is folded at finalize
            # once both streams have closed, so we hold the raw event until then.
            self._primary_error_obj = obj
        else:
            self._primary = res
            self._primary_done.set()
        self._close_stdin()

    def _close_stdin(self) -> None:
        try:
            stdin = self._proc.stdin
            if stdin is not None and not stdin.closed:
                stdin.close()
        except Exception:
            pass

    def _finalize(self) -> None:
        self._stop.set()  # release the watchdog
        if self._stderr_thread.is_alive():
            self._stderr_thread.join(timeout=2.0)
        stderr = "".join(self._stderr_parts).strip()

        with self._lock:
            self._closed = True

        if self._primary_error_obj is not None:
            self._primary = parse_result_event(self._primary_error_obj, self._session_id, returncode=1, stderr=stderr)
        elif self._primary is None:
            # Never saw a result: a crash, or the watchdog killed a hung process.
            msg = "claude stream ended without a result"
            if stderr:
                msg = f"{msg}: {stderr}"
            self._primary = ClaudeResult(text="", session_id=self._session_id, is_error=True, error=msg)
        elif stderr and self._primary.is_error and stderr not in (self._primary.error or ""):
            self._primary = replace(self._primary, error=f"{self._primary.error}: {stderr}")

        self._primary_done.set()
        self._reap()
        self._finished.set()

    def _reap(self) -> None:
        try:
            self._proc.wait(timeout=5)
        except Exception:
            self._kill()

    # -- results -----------------------------------------------------------

    def wait_primary(self, timeout: Optional[float] = None) -> Optional[ClaudeResult]:
        """Block until the reply is sendable; return it (None if it never came)."""
        self._primary_done.wait(timeout)
        return self._primary

    def wait_finished(self, timeout: Optional[float] = None) -> bool:
        """Block until the process is fully done (strays collected, error folded)."""
        return self._finished.wait(timeout)

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    @property
    def strays(self) -> list[ClaudeResult]:
        return list(self._strays)


@dataclass
class StreamDriver:
    """Launch :class:`StreamTurn` processes, reusing a driver's command + env."""

    driver: ClaudeDriver
    idle_timeout: float = 300.0
    total_timeout: float = 1800.0
    spawn: Callable[[Sequence[str], dict], object] = _default_spawn

    def start(
        self,
        prompt: str,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> StreamTurn:
        if self.spawn is _default_spawn and shutil.which(self.driver.claude_bin) is None:
            raise ClaudeError(
                f"claude binary not found on PATH: {self.driver.claude_bin!r}. "
                "Install Claude Code and sign in to your subscription first."
            )
        cmd = self.driver.build_command(session_id, model, stream=True)
        proc = self.spawn(cmd, _child_env(self.driver.disable_auto_memory))
        turn = StreamTurn(
            proc,
            fallback_session_id=session_id,
            idle_timeout=self.idle_timeout,
            total_timeout=self.total_timeout,
        )
        return turn.start(prompt)
