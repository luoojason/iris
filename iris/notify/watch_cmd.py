"""Run a command transparently and ping the user when it finishes or fails.

The wrapped command's stdout/stderr pass straight through and its exit code is
preserved, so `iris watch -- <cmd>` is a safe drop-in prefix. The child shares
this process's group, so a terminal Ctrl-C reaches it. A model call happens at
most once, only on a gated failure.
"""

from __future__ import annotations

import collections
import signal
import subprocess
import sys
import time

from .compose import render
from .deliver import send as deliver_send
from .events import Event
from .gate import decide, needs_model


def run_command(argv, runner=None, clock=time.monotonic):
    """Run argv with streaming passthrough. Returns (exit_code, duration_s, tail).

    ``runner`` is a test seam: when given, it is called with argv and must return
    the (exit_code, duration_s, tail) tuple instead of spawning a real process.
    """
    if runner is not None:
        return runner(argv)
    start = clock()
    proc = subprocess.Popen(
        list(argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    tail = collections.deque(maxlen=50)
    # Forward SIGTERM to the child so it is not orphaned if iris is killed. The
    # child already shares our process group, so a terminal Ctrl-C reaches it too.
    prev_term = None
    try:
        prev_term = signal.signal(signal.SIGTERM, lambda *_: proc.terminate())
    except (ValueError, OSError):
        prev_term = None  # not the main thread; rely on the shared process group
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            tail.append(line)
        proc.wait()
    except KeyboardInterrupt:
        # The child already got SIGINT via the shared group; reap it and keep its
        # exit code instead of letting the traceback escape and drop the code.
        proc.wait()
    finally:
        if prev_term is not None:
            try:
                signal.signal(signal.SIGTERM, prev_term)
            except (ValueError, OSError):
                pass
    return proc.returncode, clock() - start, "".join(tail)


def build_notify_driver(config):
    """A one-shot driver for failure triage: notify persona, no retries, short timeout."""
    from ..driver import ClaudeDriver

    return ClaudeDriver(
        claude_bin=config.claude_bin,
        model=config.model,
        append_system_prompt_file=config.notify_persona or config.persona_file,
        timeout=60,
        max_retries=0,
        timeout_max_retries=0,
    )


class _RecordingDriver:
    """Wrap the triage driver so its one model call lands in the usage ledger."""

    def __init__(self, inner, usage_file: str):
        self._inner = inner
        self._usage_file = usage_file

    def run(self, prompt, session_id=None, model=None):
        result = self._inner.run(prompt, session_id, model)
        from ..usage import record_turn

        record_turn(self._usage_file, "notify", result)
        return result


def _guard_parked(config) -> bool:
    """True when the credit guard says the month is nearly spent.

    The failure-triage call is a nicety, and watch runs can fire from the
    clock (scheduled script rules), so at park level the templated line goes
    out without spending a model call. Fail-open: a broken ledger must not
    silence triage for attended runs.
    """
    try:
        from ..usage import CreditGuard

        return CreditGuard.from_config(config).should_park()
    except Exception:
        return False


def watch(argv, config, *, name=None, force=False, quiet=False, fold=False,
          resume=False, channel=None, runner=None, driver_factory=None, sender=None):
    """Run the command, decide, compose, deliver. Returns the command's exit code.

    With ``fold=True`` a concise completion note is also appended to the
    fold-back inbox, so an Iris-launched background command (run_in_background)
    surfaces in her next turn's context and she can continue the plan with the
    result in hand. The owner still has to send that next message: the fold path
    alone starts no model turn (zero idle inference).

    With ``resume=True`` AND the owner having turned autonomous resume on
    (``IRIS_AUTO_RESUME``), a resume request is also enqueued so the bot fires
    one follow-up turn on that conversation — the chain carries itself forward.
    This is the bounded relaxation of zero idle inference; it is inert unless
    both the per-launch flag and the master flag are set.

    ``channel`` is the conversation the command was launched from (a thread's
    id, threaded through from ``IRIS_ORIGIN_CHANNEL`` by run_in_background). When
    set, the completion ping, the fold note, and the resume turn all go BACK
    THERE, so a task started in a thread does not surface its result over in the
    home channel. A clock-launched run (a scheduled script rule) has no origin
    and falls back to the home / notify channel as before.
    """
    exit_code, duration_s, tail = run_command(argv, runner=runner)
    title = name or " ".join(argv)
    status = "finished" if exit_code == 0 else f"failed (exit {exit_code})"
    origin = (channel or "").strip() or None
    # Where the conversation-facing outputs (fold note, resume turn) land: the
    # originating thread if there is one, else the owner's home channel.
    conv_channel = origin or getattr(config, "home_channel", "")
    if fold and getattr(config, "inbox_file", ""):
        note = f"background command '{title}' {status}."
        if (tail or "").strip():
            note += " Last output: " + (tail or "").strip()[-400:]
        try:
            from ..inbox import Inbox
            Inbox(config.inbox_file).append(
                note, conversation_id=(f"discord:{conv_channel}" if conv_channel else None))
        except Exception:
            pass
    if resume and getattr(config, "auto_resume", False) and conv_channel:
        prompt = (
            f"[auto] Your background task '{title}' just {status}. "
            "If the plan has a next step, do it now, then tell me what happened."
        )
        if (tail or "").strip():
            prompt += " Last output: " + (tail or "").strip()[-400:]
        try:
            from ..autoresume import ResumeQueue
            ResumeQueue(config.resume_queue_file).enqueue(
                f"discord:{conv_channel}", prompt)
        except Exception:
            pass
    event = Event(
        source="command",
        kind="finished",
        title=title,
        exit_code=exit_code,
        duration_s=duration_s,
        tail=tail,
    )
    verdict = decide(event, config.watch_min_seconds, force=force, quiet=quiet)
    if verdict == "notify":
        driver = None
        if needs_model(event) and not _guard_parked(config):
            driver = driver_factory() if driver_factory is not None else build_notify_driver(config)
            if driver is not None:
                driver = _RecordingDriver(driver, config.usage_file)
        text = render(event, driver)
        # Ping the originating thread when there is one, else the notify channel.
        notify_channel = origin or config.notify_channel
        if not deliver_send(text, token=config.discord_token, channel=notify_channel, sender=sender):
            print(text)
    return exit_code
