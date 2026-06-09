"""Run a command transparently and ping the user when it finishes or fails.

The wrapped command's stdout/stderr pass straight through and its exit code is
preserved, so `iris watch -- <cmd>` is a safe drop-in prefix. The child shares
this process's group, so a terminal Ctrl-C reaches it. A model call happens at
most once, only on a gated failure.
"""

from __future__ import annotations

import collections
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
        bufsize=1,
    )
    tail = collections.deque(maxlen=50)
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        tail.append(line)
    proc.wait()
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


def watch(argv, config, *, name=None, force=False, quiet=False,
          runner=None, driver_factory=None, sender=None):
    """Run the command, decide, compose, deliver. Returns the command's exit code."""
    exit_code, duration_s, tail = run_command(argv, runner=runner)
    event = Event(
        source="command",
        kind="finished",
        title=name or " ".join(argv),
        exit_code=exit_code,
        duration_s=duration_s,
        tail=tail,
        urgency="high" if exit_code != 0 else "normal",
    )
    verdict = decide(event, config.watch_min_seconds, force=force, quiet=quiet)
    if verdict == "notify":
        driver = None
        if needs_model(event):
            driver = driver_factory() if driver_factory is not None else build_notify_driver(config)
        text = render(event, driver)
        if not deliver_send(text, token=config.discord_token, channel=config.notify_channel, sender=sender):
            print(text)
    return exit_code
