# Event wakes

Date: 2026-06-10
Status: approved

## Problem

Reminders fire at a *time*. Plenty of things the owner cares about fire on an
*event*: a build log gains an ERROR line, a file lands in a drop folder, a
lock file disappears, a long-running export finally changes size. Today the
owner polls those by hand.

Event wakes let the owner declare such conditions once; the existing
`reminders-tick` cadence evaluates them cheaply and *wakes the owner* (a
Discord ping plus a fold-back note for the agent's next turn) when one fires.

**A wake never calls the model.** The tick is a clock, and no model call may
fire from a clock. "Waking the agent" means: the owner is pinged with the
rule's message, and the event is queued in the inbox so the agent's very next
owner-initiated turn knows about it. The model call, if any follows, is the
owner's.

## Rules file (`IRIS_WAKES_FILE`, default `iris-wakes.json`)

Owner-authored JSON list. The model has no tool to create, edit, or list
rules in v1 — rules contain filesystem paths, and the model never names
paths. (A later read-only `list_wakes` tool may expose names and states,
never paths.)

```json
[
  {
    "name": "build-errors",
    "kind": "log_pattern",
    "path": "/home/you/myrepo/run.log",
    "pattern": "ERROR|Traceback",
    "message": "the build run hit an error",
    "channel_id": "",
    "cooldown_secs": 3600,
    "once": false
  }
]
```

Fields:

- `name` — unique id for the rule, `[a-z0-9][a-z0-9-_]*`, max 32 chars.
- `kind` — one of:
  - `file_exists` — fires when `path` exists (edge-triggered: fires on the
    transition from absent to present).
  - `file_gone` — the reverse transition.
  - `file_changed` — fires when `path`'s (mtime, size) changes from the last
    observed value. The first observation arms the rule without firing.
  - `log_pattern` — fires when new bytes appended to `path` since the last
    observed offset contain regex `pattern`. Only the appended tail is read
    (max 256 KB per tick per rule); a truncated/rotated file (size shrank)
    re-arms from offset 0 without firing on the old content.
- `path` — absolute path, owner-written.
- `pattern` — regex, `log_pattern` only. Compiled with `re.IGNORECASE` off;
  invalid regexes are a doctor error and the rule is skipped at tick time
  (with one warning line in the tick output, not silence).
- `message` — the pre-written text to deliver. The ping is
  `wake <name>: <message>` plus, for `log_pattern`, the first matching line
  (truncated to 200 chars).
- `channel_id` — optional recorded channel override; empty means the home
  channel (`IRIS_DISCORD_HOME_CHANNEL`).
- `cooldown_secs` — minimum seconds between fires of the same rule
  (default 3600). Absorbs flapping conditions.
- `once` — when true the rule disarms after its first fire (state records
  `fired_once`); the owner re-arms by deleting that state entry or renaming
  the rule.

## State file (`IRIS_WAKES_STATE`, default `iris-wakes.state.json`)

Written by the tick, never by the owner. Keyed by rule name:

```json
{
  "build-errors": {
    "present": true,
    "mtime": 0.0, "size": 0,
    "offset": 1024,
    "last_fired_ts": 0.0,
    "fired_once": false
  }
}
```

State for rules that no longer exist in the rules file is pruned on each
tick. Corrupt state starts fresh (`.corrupt` sidecar, same as SessionStore).

## tick_wakes

`tick_wakes(config, now=None) -> str` lives in `iris/wakes.py` and is called
from `iris reminders-tick` beside `budget_tick`. Per tick it:

1. Loads rules; a missing rules file means "0 rules" and is silent.
   A malformed rules file is reported (one line) and skipped — never a crash
   that would take reminder delivery down with it.
2. Evaluates each rule against the state under the wakes flock (the tick may
   overlap a slow predecessor; the lock makes read-evaluate-write atomic).
3. For each fire: sends the ping over Discord REST, queues the same line in
   the inbox for fold-back, updates `last_fired_ts`. A failed Discord send
   still updates observation state but *not* `last_fired_ts`, so the next
   tick retries the ping (the inbox entry is queued exactly once).
4. Saves state atomically, returns a one-line summary
   (`wakes: N rules, M fired`) that the CLI prints.

Evaluation does stat/read calls only. No subprocess, no network besides the
ping, no model.

## doctor

`doctor` gains a wakes section when `IRIS_WAKES_FILE` exists: it validates
the rules (unique well-formed names, known kinds, absolute paths, regex
compiles, positive cooldown, message present) and prints either `wakes: N
rules ok` or each problem. Validation is pure (`validate_rules(rules) ->
list[str]` of problems) so tests need no filesystem.

## Config knobs

- `IRIS_WAKES_FILE` (default `iris-wakes.json`)
- `IRIS_WAKES_STATE` (default `iris-wakes.state.json`)

Both on `Config` (`wakes_file`, `wakes_state`), documented in README and
`.env.example`.

## Invariants

- Zero model calls anywhere in this module.
- A wake failure (bad rule, unreadable path, Discord down) degrades to a
  logged line; reminder delivery in the same tick is never affected.
- Pings carry owner-authored text plus matched log lines only.
