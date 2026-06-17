# Quiet heartbeat: a silent-by-default health checklist

## Goal

Give the owner a standing health watch that is quiet when things are fine and
speaks only when they are not. Hermes/OpenClaw have "heartbeat" pings that
periodically confirm the agent is alive; the higher-quality version is the
inverse — report by exception. The owner lists conditions that should hold (disk
free, a backup file is fresh, a URL is up); the tick checks them every cadence and
sends ONE consolidated message only when the set of failing checks changes.

Like wakes and scheduled jobs, the checklist is **owner-authored**
(`IRIS_HEARTBEAT_FILE`); the model has no tool to write it. And like wakes, the
tick **never calls the model**: a failure is a pre-written ping plus a fold-back
inbox note. This is not a relaxation of zero-idle-inference — no inference happens.

## Wakes vs. heartbeat

They look adjacent but answer different questions:

- **Wakes** are *edge-triggered, per-rule*: "tell me WHEN this log gains an error /
  this URL changes." Each rule fires its own ping on its own event.
- **Heartbeat** is *level-triggered, aggregated*: "is everything that should be
  true true right now?" One consolidated digest for the whole checklist, sent only
  when the failing set changes — so a healthy system, and a steady known failure,
  are both silent.

## Check kinds (cheap, deterministic, model-free)

- `disk_free` — `{path, min_percent}`: fail if free space on the filesystem is
  below the floor.
- `file_fresh` — `{path, max_age_secs}`: fail if the file is missing or last
  changed longer ago than the limit. (Proof a backup/cron/export actually ran.)
- `url_ok` — `{url, expect_status?=200}`: fail if a bounded HTTP GET returns a
  different status or is unreachable. Only the status code is read, never the body.

A check that errors (can't stat, unreachable) is a *failure with detail*, never a
raise; one poisoned check never aborts the others.

## Tick (rides `reminders-tick`)

1. No checks file → no-op.
2. Evaluate every valid check; collect `failures = {name: detail}`. Invalid or
   duplicate checks are skipped and named in the tick's log line.
3. Under a flock, compare `current = set(failures)` to the stored `failing` set:
   - **changed** → build one digest (the current failures, plus a "recovered:"
     line for names that left the set; "all clear" when `current` is empty), ping
     the home channel once, and fold the digest into the inbox.
   - **unchanged** → silent (no ping). Steady-healthy and steady-broken are both
     quiet.
4. Save `current` as the new `failing` set (inside the lock, so a crash can't
   replay a ping).

## Surfaces

- Owner CLI: `iris heartbeat` evaluates once and prints each check's status
  (read-only — no ping, no state change), and `iris doctor` validates the file.
- No chat MCP tool: the checklist is owner-authored only, like wakes.

## Deploy

Author `IRIS_HEARTBEAT_FILE` and the existing `reminders-tick` cron picks it up;
no separate timer, no enable flag (inert until the file exists). Set
`IRIS_DISCORD_HOME_CHANNEL` so a failure has somewhere to ping.

## Out of scope

A periodic "all good" confirmation ping (the whole point is silence-by-default;
add a flag if a liveness heartbeat is wanted later); process/port checks (brittle
across hosts — `file_fresh` on a heartbeat file a service touches covers the common
case); per-check cooldowns (the failing-set edge-trigger already prevents spam).
