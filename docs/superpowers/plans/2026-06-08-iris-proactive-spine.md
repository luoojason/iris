# Iris proactive spine + job-done watcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `iris watch -- <command>`, a drop-in prefix that runs a command unchanged and pings you on Discord when it finishes or fails, on a reusable proactive spine.

**Architecture:** A new `iris/notify/` package with four isolated units (event, gate, compose, deliver) plus a command-wrapper watcher. The watcher runs the command, builds a normalized Event, and the pure gate decides whether to notify. Routine successes are free templated lines; only a gated failure spends one one-shot model call (driver directly, no chat session) to read the error tail in Iris's voice. No daemon and no poll loop, preserving zero-idle-inference.

**Tech Stack:** Python 3.10+, stdlib `subprocess`/`dataclasses`/`argparse`, the existing `iris.driver.ClaudeDriver` and `iris.reminders.send_discord_message`, `pytest` with injected fakes (no real subprocess, model, or Discord in tests).

Spec: `docs/superpowers/specs/2026-06-08-iris-proactive-spine-design.md`

---

## File structure

- Create `iris/notify/__init__.py` - package marker.
- Create `iris/notify/events.py` - the `Event` dataclass (normalized currency).
- Create `iris/notify/gate.py` - `decide`, `needs_model` (pure, no I/O, no model).
- Create `iris/notify/compose.py` - `render` (template, or one model call on failure).
- Create `iris/notify/deliver.py` - `send` (Discord, injectable sender).
- Create `iris/notify/watch_cmd.py` - `run_command`, `build_notify_driver`, `watch`.
- Modify `iris/config.py` - add `notify_channel`, `watch_min_seconds`, `notify_persona`.
- Modify `iris/cli.py` - add the `watch` subcommand.
- Modify `.env.example` - document the new vars.
- Create `tests/test_notify_gate.py`, `tests/test_notify_compose.py`, `tests/test_notify_deliver.py`, `tests/test_notify_watch.py`.
- Modify `tests/test_config.py`, `tests/test_cli.py`.

---

### Task 1: Config fields for notify

**Files:**
- Modify: `iris/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_from_env_reads_notify_fields(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IRIS_NOTIFY_CHANNEL", "999")
    monkeypatch.setenv("IRIS_WATCH_MIN_SECONDS", "10")
    monkeypatch.setenv("IRIS_NOTIFY_PERSONA", "notify.md")
    cfg = Config.from_env(dotenv=tmp_path / "none.env")
    assert cfg.notify_channel == "999"
    assert cfg.watch_min_seconds == 10.0
    assert cfg.notify_persona == "notify.md"


def test_notify_defaults():
    cfg = Config()
    assert cfg.notify_channel == ""
    assert cfg.watch_min_seconds == 30.0
    assert cfg.notify_persona is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py::test_from_env_reads_notify_fields -v`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'notify_channel'`

- [ ] **Step 3: Add the fields and env reads**

In `iris/config.py`, inside the `Config` dataclass, after the `session_store_path` / `metrics_file` block (near the other state fields), add:

```python
    # Proactive notifications (iris watch). notify_channel is the Discord channel
    # or DM to ping; watch_min_seconds is the success-ping threshold so quick
    # commands stay silent; notify_persona is an optional voice for proactive
    # messages (falls back to persona_file).
    notify_channel: str = ""
    watch_min_seconds: float = 30.0
    notify_persona: Optional[str] = None
```

In `Config.from_env`, alongside the other `os.environ.get` reads, add:

```python
            notify_channel=os.environ.get("IRIS_NOTIFY_CHANNEL", ""),
            watch_min_seconds=float(os.environ.get("IRIS_WATCH_MIN_SECONDS", "30")),
            notify_persona=os.environ.get("IRIS_NOTIFY_PERSONA") or None,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (all config tests, including the two new ones)

- [ ] **Step 5: Commit**

```bash
git add iris/config.py tests/test_config.py
git commit -m "Add notify config (channel, success threshold, persona)"
```

---

### Task 2: Event and gate

**Files:**
- Create: `iris/notify/__init__.py`
- Create: `iris/notify/events.py`
- Create: `iris/notify/gate.py`
- Test: `tests/test_notify_gate.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_notify_gate.py`:

```python
"""Tests for the proactive-notify gate (pure: no I/O, no model)."""

from __future__ import annotations

from iris.notify.events import Event
from iris.notify.gate import decide, needs_model


def ev(exit_code=0, duration_s=1.0):
    return Event(source="command", kind="finished", title="job",
                 exit_code=exit_code, duration_s=duration_s)


def test_failure_always_notifies():
    assert decide(ev(exit_code=1, duration_s=0.1), 30) == "notify"


def test_quick_success_drops():
    assert decide(ev(exit_code=0, duration_s=2), 30) == "drop"


def test_long_success_notifies():
    assert decide(ev(exit_code=0, duration_s=45), 30) == "notify"


def test_always_flag_forces_notify():
    assert decide(ev(exit_code=0, duration_s=1), 30, force=True) == "notify"


def test_quiet_flag_forces_drop():
    assert decide(ev(exit_code=1, duration_s=99), 30, quiet=True) == "drop"


def test_needs_model_only_on_failure():
    assert needs_model(ev(exit_code=1)) is True
    assert needs_model(ev(exit_code=0)) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_notify_gate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'iris.notify'`

- [ ] **Step 3: Create the package, Event, and gate**

Create `iris/notify/__init__.py`:

```python
"""Proactive notifications: watch things and ping the user when they matter."""
```

Create `iris/notify/events.py`:

```python
"""The normalized event every watcher emits and the gate and composer consume."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Event:
    source: str          # which watcher produced it, e.g. "command"
    kind: str            # what happened, e.g. "finished"
    title: str           # human label, e.g. the command or its --name
    exit_code: int
    duration_s: float
    tail: str = ""       # last lines of output, for failure triage
    urgency: str = "normal"   # "normal" | "high"
    detail: str = ""
```

Create `iris/notify/gate.py`:

```python
"""Decide whether an event is worth a ping, and whether it needs the model.

Pure functions, no I/O and no model call: this is the noise-and-cost control
point. ``fold`` is reserved for the future briefing and is not emitted yet.
"""

from __future__ import annotations

from .events import Event


def decide(event: Event, min_seconds: float, force: bool = False, quiet: bool = False) -> str:
    """Return "notify" or "drop" for this event."""
    if quiet:
        return "drop"
    if force:
        return "notify"
    if event.exit_code != 0:
        return "notify"
    if event.duration_s >= min_seconds:
        return "notify"
    return "drop"


def needs_model(event: Event) -> bool:
    """True when the event carries judgment worth one model call (a failure)."""
    return event.exit_code != 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_notify_gate.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add iris/notify/__init__.py iris/notify/events.py iris/notify/gate.py tests/test_notify_gate.py
git commit -m "Add the notify Event and gate"
```

---

### Task 3: Composer

**Files:**
- Create: `iris/notify/compose.py`
- Test: `tests/test_notify_compose.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_notify_compose.py`:

```python
"""Tests for the notify composer (template, or one model call on failure)."""

from __future__ import annotations

from iris.notify import compose
from iris.notify.events import Event


class FakeResult:
    def __init__(self, text, is_error=False):
        self.text = text
        self.is_error = is_error


class FakeDriver:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def run(self, prompt, session_id=None, model=None):
        self.calls.append(prompt)
        return self._result


def ev(exit_code=0, duration_s=134.0, tail=""):
    return Event(source="command", kind="finished", title="npm test",
                 exit_code=exit_code, duration_s=duration_s, tail=tail)


def test_success_uses_template_no_model():
    assert compose.render(ev(exit_code=0, duration_s=134), None) == "done: npm test passed in 2m14s"


def test_failure_template_when_no_driver():
    assert compose.render(ev(exit_code=1, duration_s=40), None) == "failed: npm test exited 1 after 40s"


def test_failure_uses_model_and_passes_tail():
    driver = FakeDriver(FakeResult("Looks like the DB migration failed. Want me to look?"))
    out = compose.render(ev(exit_code=1, tail="ERROR: relation does not exist"), driver)
    assert "DB migration" in out
    assert "ERROR: relation does not exist" in driver.calls[0]  # tail reached the prompt


def test_model_error_falls_back_to_template():
    driver = FakeDriver(FakeResult("", is_error=True))
    assert compose.render(ev(exit_code=1, duration_s=40), driver) == "failed: npm test exited 1 after 40s"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_notify_compose.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'iris.notify.compose'`

- [ ] **Step 3: Create the composer**

Create `iris/notify/compose.py`:

```python
"""Turn an event into the message Iris sends.

Routine events are a free templated line. Only when the gate says the event
needs the model (a failure) and a driver is provided do we spend one one-shot
call to read the output in Iris's voice. The model never blocks a notification:
any error falls back to the template.
"""

from __future__ import annotations

from .events import Event
from .gate import needs_model


def _fmt(seconds: float) -> str:
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m{total % 60:02d}s"


def _template(event: Event) -> str:
    if event.exit_code == 0:
        return f"done: {event.title} passed in {_fmt(event.duration_s)}"
    return f"failed: {event.title} exited {event.exit_code} after {_fmt(event.duration_s)}"


def _failure_prompt(event: Event) -> str:
    return (
        "A command the user was running just failed. In your own voice, in one or "
        "two short sentences, tell them what likely went wrong and offer to look "
        "closer. Be specific if the output makes the cause clear.\n\n"
        f"Command: {event.title}\n"
        f"Exit code: {event.exit_code}\n"
        f"Duration: {_fmt(event.duration_s)}\n"
        f"Last output:\n{event.tail or '(no output captured)'}"
    )


def render(event: Event, driver) -> str:
    """Return the message text. ``driver`` is None for routine events."""
    if driver is None or not needs_model(event):
        return _template(event)
    try:
        result = driver.run(_failure_prompt(event), session_id=None)
    except Exception:
        return _template(event)
    if getattr(result, "is_error", True) or not (getattr(result, "text", "") or "").strip():
        return _template(event)
    return result.text.strip()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_notify_compose.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add iris/notify/compose.py tests/test_notify_compose.py
git commit -m "Add the notify composer (template, model on failure)"
```

---

### Task 4: Delivery

**Files:**
- Create: `iris/notify/deliver.py`
- Test: `tests/test_notify_deliver.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_notify_deliver.py`:

```python
"""Tests for notify delivery (Discord sender injected)."""

from __future__ import annotations

from iris.notify import deliver


def test_no_target_returns_false():
    assert deliver.send("hi", token="", channel="123") is False
    assert deliver.send("hi", token="t", channel="") is False


def test_sends_via_injected_sender():
    calls = []

    def fake(channel, text, token):
        calls.append((channel, text, token))
        return True

    assert deliver.send("hello", token="t", channel="123", sender=fake) is True
    assert calls == [("123", "hello", "t")]


def test_sender_exception_is_false():
    def boom(channel, text, token):
        raise RuntimeError("network down")

    assert deliver.send("x", token="t", channel="123", sender=boom) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_notify_deliver.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'iris.notify.deliver'`

- [ ] **Step 3: Create delivery**

Create `iris/notify/deliver.py`:

```python
"""Send a proactive message out. Discord for now; Telegram can be added later."""

from __future__ import annotations


def send(text: str, *, token: str, channel: str, sender=None) -> bool:
    """Deliver ``text`` to Discord. Returns False if unconfigured or it fails,
    so the caller can fall back to printing locally."""
    if not token or not channel:
        return False
    if sender is None:
        from ..reminders import send_discord_message
        sender = send_discord_message
    try:
        return bool(sender(channel, text, token))
    except Exception:
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_notify_deliver.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add iris/notify/deliver.py tests/test_notify_deliver.py
git commit -m "Add notify delivery over Discord"
```

---

### Task 5: The command watcher

**Files:**
- Create: `iris/notify/watch_cmd.py`
- Test: `tests/test_notify_watch.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_notify_watch.py`:

```python
"""Tests for the command-wrapper watcher (subprocess + Discord faked)."""

from __future__ import annotations

from iris.config import Config
from iris.notify import watch_cmd


def fake_runner(result):
    # result is (exit_code, duration_s, tail)
    return lambda argv: result


def collect_sender(sent):
    def sender(channel, text, token):
        sent.append((channel, text, token))
        return True
    return sender


def test_exit_code_passthrough_and_quick_success_is_silent():
    cfg = Config(discord_token="t", notify_channel="123", watch_min_seconds=30)
    sent = []
    rc = watch_cmd.watch(["true"], cfg, runner=fake_runner((0, 2.0, "")),
                         sender=collect_sender(sent))
    assert rc == 0
    assert sent == []  # quick success dropped, nothing delivered


def test_long_success_notifies_with_template():
    cfg = Config(discord_token="t", notify_channel="123", watch_min_seconds=30)
    sent = []
    rc = watch_cmd.watch(["build.sh"], cfg, name="build",
                         runner=fake_runner((0, 45.0, "")),
                         sender=collect_sender(sent))
    assert rc == 0
    assert sent == [("123", "done: build passed in 45s", "t")]


def test_failure_uses_driver_and_delivers():
    cfg = Config(discord_token="t", notify_channel="123")
    sent = []

    class FakeResult:
        text = "Migration failed, sir."
        is_error = False

    class FakeDriver:
        def run(self, prompt, session_id=None, model=None):
            return FakeResult()

    rc = watch_cmd.watch(["deploy.sh"], cfg, name="deploy",
                         runner=fake_runner((1, 5.0, "ERR: boom")),
                         driver_factory=lambda: FakeDriver(),
                         sender=collect_sender(sent))
    assert rc == 1
    assert sent == [("123", "Migration failed, sir.", "t")]


def test_falls_back_to_print_when_no_delivery(capsys):
    cfg = Config()  # no token or channel
    rc = watch_cmd.watch(["job.sh"], cfg, name="job",
                         runner=fake_runner((1, 5.0, "boom")),
                         driver_factory=lambda: None)
    assert rc == 1
    assert "failed: job exited 1 after 5s" in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_notify_watch.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'iris.notify.watch_cmd'`

- [ ] **Step 3: Create the watcher**

Create `iris/notify/watch_cmd.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_notify_watch.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add iris/notify/watch_cmd.py tests/test_notify_watch.py
git commit -m "Add the iris watch command wrapper"
```

---

### Task 6: CLI wiring and docs

**Files:**
- Modify: `iris/cli.py`
- Modify: `.env.example`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli.py`:

```python
def test_watch_without_command_errors():
    from iris.cli import main
    assert main(["watch"]) == 2  # no command after watch -> usage, exit 2


def test_watch_runs_command_and_returns_its_code(monkeypatch):
    from iris.cli import main
    import iris.notify.watch_cmd as wc
    seen = {}

    def fake_watch(argv, config, **kwargs):
        seen["argv"] = argv
        seen["name"] = kwargs.get("name")
        return 7

    monkeypatch.setattr(wc, "watch", fake_watch)
    rc = main(["watch", "--name", "build", "--", "npm", "test"])
    assert rc == 7
    assert seen["argv"] == ["npm", "test"]
    assert seen["name"] == "build"
```

Also extend `test_module_help_runs` to include `"watch"` in the command loop:

```python
    for command in ("discord", "telegram", "chat", "doctor", "watch"):
        assert command in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_cli.py -v`
Expected: FAIL (`test_watch_without_command_errors` returns the discord path, and `--help` does not list `watch`)

- [ ] **Step 3: Wire the subcommand**

In `iris/cli.py`, in `main`, add the subparser next to the others (after the `doctor` parser block):

```python
    watch_parser = sub.add_parser("watch", help="run a command and ping you when it finishes")
    watch_parser.add_argument("--name", default=None, help="label for the notification")
    watch_parser.add_argument("--always", action="store_true", help="ping even on a quick success")
    watch_parser.add_argument("--quiet", action="store_true", help="suppress the ping for this run")
    watch_parser.add_argument("command", nargs=argparse.REMAINDER, help="-- then the command to run")
```

In `iris/cli.py`, in `main`, add the dispatch branch before the final `# discord` block:

```python
    if command == "watch":
        from .notify.watch_cmd import watch as run_watch
        cmd = list(args.command)
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            print("usage: iris watch [--name N] [--always] [--quiet] -- <command>")
            return 2
        return run_watch(cmd, config, name=args.name, force=args.always, quiet=args.quiet)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_cli.py -v`
Expected: PASS

- [ ] **Step 5: Document the env vars**

In `.env.example`, under the `--- State ---` section (after `IRIS_TIMEOUT_RETRIES`), add:

```bash
# --- Proactive notifications (iris watch) ---
# `iris watch -- <command>` runs a command and pings you when it finishes or
# fails. Channel to ping (a Discord channel or DM id); reuses IRIS_DISCORD_TOKEN.
IRIS_NOTIFY_CHANNEL=
# Only ping on a SUCCESS if it ran at least this many seconds (failures always
# ping). Keeps quick commands quiet.
IRIS_WATCH_MIN_SECONDS=30
# Optional voice for proactive messages; falls back to IRIS_PERSONA_FILE.
IRIS_NOTIFY_PERSONA=
```

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (all prior tests plus the new notify tests)

- [ ] **Step 7: Commit**

```bash
git add iris/cli.py .env.example tests/test_cli.py
git commit -m "Wire the iris watch subcommand and document its env vars"
```

---

## Manual verification (after the plan)

Not a test step, run once by hand to confirm the real path:

```bash
export IRIS_DISCORD_TOKEN=...        # your bot token
export IRIS_NOTIFY_CHANNEL=...       # your DM or home channel id
iris watch -- sh -c "sleep 35; true"     # long success -> a "done:" ping
iris watch -- sh -c "echo boom 1>&2; exit 1"  # failure -> an Iris-voiced ping
iris watch -- true                        # quick success -> silent
```
