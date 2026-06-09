# Iris proactive spine + job-done watcher

Status: approved design, ready for implementation planning
Date: 2026-06-08

## Why

Iris today is purely reactive: she answers only when a message arrives. The next
step is to let her reach out first when something worth your attention happens.
This is the first slice of a larger "proactive Iris" vision (job/CI notices,
change monitoring, a morning briefing, and a consistent personality across all of
them). Those scenarios are the same machine with different inputs, so this spec
builds that machine (the spine) and proves it end to end with the simplest, most
useful input: telling you when a command you ran finishes or fails.

## Goals

- A drop-in command prefix, `iris watch -- <command>`, that runs your command
  unchanged and pings you on Discord when it finishes, with the ping gated so
  routine fast commands stay silent.
- A reusable spine (event, gate, composer, delivery) that later watchers and the
  briefing plug into without rework.
- Preserve the zero-idle-inference shape: no daemon, no poll loop, and a model
  call only when there is genuine judgment to add.

## Non-goals (handled in later sub-projects)

- Webhook or polling watchers (CI cloud runs, repo issues, prices, uptime, email).
- The scheduled morning briefing (it will consume "fold" events from the gate).
- A persistent notification daemon or a web server.

## UX

```
iris watch -- npm test
iris watch --name "nightly deploy" -- ./deploy.sh
iris watch --always -- ./quick.sh      # force a ping even if fast and successful
iris watch --quiet -- ./noisy.sh        # suppress the ping for this run
```

Iris runs the command transparently: stdout and stderr pass straight through, the
exit code is preserved, and Ctrl-C and other signals forward to the child. It
behaves exactly like running the command bare, so it is safe to prefix anything.
It additionally times the run, retains the tail of combined output, and on exit
decides whether to notify you. The notification arrives on Discord; the terminal
experience is unchanged.

## Architecture

A new package `iris/notify/`, four isolated units plus the watcher entry point.
Each unit has one purpose and a small interface, so later watchers reuse them and
tests can exercise each in isolation.

### events.py

The normalized currency every watcher emits and the gate and composer consume.

```
@dataclass
class Event:
    source: str        # "command"
    kind: str          # "finished"
    title: str         # e.g. the command name or --name value
    exit_code: int
    duration_s: float
    tail: str          # last ~50 lines of combined output
    urgency: str        # "normal" | "high" (high when failed)
    detail: str = ""    # optional extra context
```

Pure data, no behavior.

### gate.py

The noise-and-cost control point. Pure functions, no I/O, no model.

- `decide(event, config) -> "notify" | "fold" | "drop"`
  - failure (non-zero exit): `notify`
  - success and `duration_s >= config.watch_min_seconds`: `notify`
  - success and shorter than the threshold: `drop`
  - `--always` forces `notify`; `--quiet` forces `drop`
  - `fold` is a valid verdict reserved for the future briefing; in this slice it
    is treated as `drop` plus a debug log line, so the briefing phase can later
    consume folded events without changing the gate contract.
- `needs_model(event) -> bool`
  - true when the event carries judgment worth spending a call on (this slice:
    a failure). false for routine success.

### compose.py

The only place a model call can happen, and only when `gate.needs_model` is true.

- `render(event, config, driver) -> str`
  - routine success: a templated, Iris-toned line, no model. Example wording:
    `done: npm test passed in 2m14s`.
  - failure (`needs_model`): one `driver.run(prompt, session_id=None)` call (the
    driver directly, not a chat session), with `restrict_builtin_tools=True`,
    `max_retries=0`, and a short timeout. The prompt carries the command, the
    exit code, the duration, and the last ~50 lines, and asks Iris, in her voice,
    to say what likely broke in a sentence or two and offer to look closer.
  - if the model call errors or times out, fall back to the failure template
    (`failed: deploy exited 1 after 40s`). A notification never blocks on
    inference.
- Iris's voice comes from a notify persona: `IRIS_NOTIFY_PERSONA` if set, else
  the existing `IRIS_PERSONA_FILE`.

### deliver.py

Outbound only.

- `send(text, config) -> bool`
  - Discord via the existing `reminders.send_discord_message` to
    `config.notify_channel`.
  - Telegram is out of scope for v1 (the seam is left so it can be added the
    same way later).
  - if no delivery target is configured, return False so the caller prints the
    notice locally instead.

### watch_cmd.py

The watcher and the `iris watch` subcommand. Runs the wrapped command (subprocess
with streaming passthrough), builds the `Event`, then `gate.decide` ->
`compose.render` (only if not dropped) -> `deliver.send` (falling back to a local
print), and exits with the command's own exit code.

Flow:

```
iris watch -- cmd
  run cmd (no model), capture exit code + duration + output tail
  event = Event(...)
  verdict = gate.decide(event, cfg)
  if verdict != "notify": exit(cmd_exit_code)   # drop or fold: no send in this slice
  text = compose.render(event, cfg, driver)   # model only if gate.needs_model
  if not deliver.send(text, cfg): print(text)
  exit(cmd_exit_code)
```

## Config

- Reuse `IRIS_DISCORD_TOKEN`.
- New `IRIS_NOTIFY_CHANNEL`: the Discord channel or DM to ping.
- New `IRIS_WATCH_MIN_SECONDS` (default 30): the success-ping threshold.
- New optional `IRIS_NOTIFY_PERSONA`: a persona file for the proactive voice;
  falls back to `IRIS_PERSONA_FILE`.
- All added to `Config` and `.env.example`. If neither token nor channel is set,
  `iris watch` still runs the command and prints the notice locally.

## Zero-idle-inference guarantee

No daemon and no poll loop. The only process is your wrapped command. A model call
happens at most once per run, and only on a gated failure. Routine successes cost
nothing. The feature stays inside the agent-credit shape by construction, the same
property that the `reminders-tick` design preserves.

## Error handling

- The wrapped command's exit code, stdout, and stderr pass through unchanged. The
  wrapper never alters the job's behavior.
- Signals (Ctrl-C, SIGTERM) forward to the child so it is not orphaned.
- A delivery failure logs a warning and never affects the command's exit.
- A compose model failure falls back to the template.

## Testing

All tests use fakes, matching the repo (no real subprocess, model, or Discord).

- `gate`: pure unit tests across exit codes, durations, and the `--always` /
  `--quiet` flags, including the `fold`-as-drop behavior.
- `compose`: the template path asserts no model call; the failure path uses the
  existing `FakeDriver` to assert the output tail reaches the prompt and that a
  driver error falls back to the template.
- `deliver`: a fake sender asserts the target and content, and that an
  unconfigured target returns False.
- `watch_cmd`: a fake command runner asserts exit-code passthrough and the
  gate -> compose -> deliver wiring, with a dropped verdict making no
  compose or deliver call.

## Future plug-ins (context, not this spec)

- Webhook and polling watchers emit the same `Event`, so they reuse gate,
  compose, and deliver unchanged.
- The morning briefing is a scheduled job that aggregates `fold` events plus a
  few live reads into one composer call.
- Personality deepens in `compose` (voice plus a relationship-memory convention),
  shared by every proactive message.
