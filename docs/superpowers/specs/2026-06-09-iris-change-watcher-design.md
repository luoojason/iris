# Iris change-watcher (poll and diff)

Status: approved design, ready for implementation planning
Date: 2026-06-09

## Why

The proactive spine (`iris/notify/`) and the `iris watch -- <command>` wrapper ship
the first watcher: it reacts to a command you ran. This sub-project adds the
second watcher family: noticing when something out in the world changes, even
when you did not start it. It is the "change monitoring" scenario from the
proactive-Iris vision (a site goes down, a price moves, a repo gets a new
release, a file changes). Rather than build one adapter per source, it is one
general fetch-and-diff engine, because web pages, JSON APIs, git, and local
state all reduce to: fetch a value on a schedule, extract the part you care
about, and compare it to what it was last time.

## Goals

- Register watches from the CLI, stored in a file, and have a cron/timer-driven
  `iris watch-tick` notice changes and ping you on Discord through the existing
  spine (gate, compose, deliver).
- Cover web pages, JSON APIs, git, and local state with two fetch backends
  (HTTP GET, shell command) and a small set of extractors.
- Preserve zero-idle-inference: the fetch and diff make no model call; only a
  real change reaches the composer, and a change uses a free template by default.

## Non-goals (later or out of scope)

- Email or chat-message watching (needs auth and a provider API, not fetch-and-diff).
- A long-running daemon or push/webhook receiver (that is the separate webhook
  watcher; this one is poll-based on a tick).
- Conditions beyond "the extracted value changed" (no numeric thresholds or
  expression language in v1).
- A model-voiced (Iris-phrased) change message, and an MCP tool for Iris to add
  watches herself. Both are clean later additions, not v1.

## UX

```
iris watch-add --name "blog" --url https://example.com/blog
iris watch-add --name "api-version" --url https://api.example.com/status --json version
iris watch-add --name "site-up" --url https://example.com --status
iris watch-add --name "price" --url https://shop.example.com/item --match 'price">\$([0-9.]+)'
iris watch-add --name "repo-head" --cmd "git ls-remote https://github.com/x/y HEAD" --every 3600
iris watch-list
iris watch-rm price
iris watch-tick          # run by cron or a systemd timer
```

The first time `watch-tick` sees a watch it records a baseline silently. On a
later tick, if the extracted value differs from the stored one, it pings you and
stores the new value. `--every SECONDS` is a minimum interval, so a one-minute
cron can host watches that only check hourly.

## Architecture

New units under `iris/notify/` (beside the spine they reuse). Each has one
responsibility and a small interface.

### watches.py

A file-backed store, mirroring `SessionStore` / `ReminderStore` (same atomic
temp-file-plus-rename write, in-process lock).

A watch is a dict:

```
{
  "name": str,
  "url": str | None,          # HTTP GET source
  "cmd": str | None,          # shell command source (stdout)
  "extract": {"kind": "text" | "json" | "regex" | "status", "arg": str},
  "every_seconds": float,     # minimum interval; 0 = every tick
  "last_value": str | None,   # None until the first sighting (baseline)
  "last_checked": float,      # epoch seconds
  "last_changed": float
}
```

`WatchStore(path)` offers `add(watch)`, `remove(name) -> bool`, `list() -> list`,
`get(name)`, `record(name, value, checked_ts, changed)` (updates last_value /
last_checked / last_changed), and `due(now) -> list` (watches whose
`last_checked + every_seconds <= now`).

### fetch.py

`fetch(watch, *, http_get=None, runner=None) -> str` returns the raw response:
- url: HTTP GET via `urllib.request` (injectable `http_get` for tests), returning
  the body text; for an `--status` extract it returns the status code as text.
- cmd: run the shell command via `subprocess` (injectable `runner` for tests),
  returning stdout. A non-zero exit returns the stderr/stdout so a broken command
  surfaces as a value change rather than a crash.

`extract(raw, watch) -> str` applies the extractor:
- `text`: the raw string, stripped.
- `json`: parse JSON and walk the dotted `arg` path, return the value as text.
- `regex`: first capture group of `arg` (or the whole match if no group); empty
  string if no match.
- `status`: the raw status string (the url backend already produced it).

Both functions are total: a fetch error (network, timeout, bad command) is
turned into a sentinel error string value, so it counts as a change (for example
a site going down) instead of throwing. A short timeout is used for HTTP and the
command.

### watch_tick.py

The engine and the `watch-add` / `watch-list` / `watch-rm` / `watch-tick` CLI
handlers.

`tick(store, config, *, now, http_get=None, runner=None, sender=None)`:

```
for watch in store.due(now):
    raw = fetch(watch, http_get=http_get, runner=runner)
    value = extract(raw, watch)
    if watch["last_value"] is None:          # first sighting: baseline, silent
        store.record(watch["name"], value, now, changed=False)
        continue
    if value == watch["last_value"]:          # no change
        store.record(watch["name"], watch["last_value"], now, changed=False)
        continue
    event = Event(source="watch", kind="changed", title=watch["name"],
                  exit_code=0, duration_s=0.0,
                  tail=watch["last_value"], detail=value, urgency="normal")
    # tail holds the OLD value, detail the NEW value, for the compose template.
    if gate.decide(event, config.watch_min_seconds) == "notify":
        text = compose.render(event, None)    # template; no model for a diff
        if not deliver.send(text, token=config.discord_token,
                            channel=config.notify_channel, sender=sender):
            print(text)
    store.record(watch["name"], value, now, changed=True)
```

The CLI handlers build/parse a watch dict from the flags, call the store, and
print confirmations. `watch-tick` constructs the store from
`IRIS_WATCHES_FILE`, runs `tick`, and prints a one-line summary.

### Spine extensions (small, expected)

- `gate.decide`: a `watch` source event resolves to "notify" (changes are
  already filtered upstream in `tick`, so reaching the gate means a real change).
  The `quiet` path still forces drop. This is a small added branch, not a rewrite.
- `compose._template`: one branch for `source == "watch"` returning
  `changed: NAME is now NEW (was OLD)`, reading NEW from `event.detail` and OLD
  from `event.tail`, each truncated to about 120 characters so a large body does
  not produce a giant message. Command events keep their existing wording.
  `needs_model` stays false for watch events (exit_code 0), so a change uses the
  template, no model call.

## Trigger and zero-idle-inference

`iris watch-tick` is invoked by cron or a systemd timer (a documented one-line
crontab, like `reminders-tick`). The tick fetches and diffs with no model call.
The composer is reached only on a real change, and even then renders a template.
So there is no idle inference and no idle process; cost scales with actual
changes, not with polling frequency.

## Config

- New `IRIS_WATCHES_FILE` (default `iris-watches.json`), added to `.gitignore`
  as local state.
- Reuses `IRIS_DISCORD_TOKEN` and `IRIS_NOTIFY_CHANNEL` for delivery (same as the
  job-done watcher). If delivery is unconfigured, `watch-tick` prints changes
  locally.
- Documented in `.env.example` and the README tools section.

## Error handling

- A fetch or extract error becomes a sentinel value (for example
  `"<error: timed out>"`), so it registers as a change and surfaces, rather than
  crashing the tick or silently swallowing a real outage.
- One watch failing never stops the others: each is handled independently inside
  the `tick` loop.
- A delivery failure prints the change locally and never aborts the tick.

## Testing

All tests inject fakes (no real network, subprocess, or Discord), matching the repo.

- `watches.py`: add / list / remove / get roundtrip; `due()` respects
  `every_seconds`; `record` updates value and timestamps; atomic write survives a
  reload.
- `fetch.py`: each extractor (text, json dotted path, regex capture, status)
  against canned raw input; the http backend with an injected `http_get`; the
  command backend with a fake `runner`; a fetch error becomes the sentinel value.
- `watch_tick.py`: first sighting records a baseline and sends nothing; an
  unchanged value sends nothing; a changed value emits and delivers
  `changed: NAME is now NEW (was OLD)`; `--every` throttling skips a
  not-yet-due watch; delivery failure falls back to a local print.
- spine: a `watch` event resolves to notify in `gate.decide`; `compose` renders
  the watch template.
- CLI: `watch-add` writes a parseable watch; `watch-list` shows it; `watch-rm`
  removes it; `watch-tick` with no watches prints an empty summary.

## Future (context, not this spec)

- A webhook watcher (push instead of poll) emits the same `Event`.
- The morning briefing consumes accumulated change events.
- A per-watch model-voiced option and an MCP tool for Iris to manage watches in chat.
