# URL watcher (merged into event wakes)

Date: 2026-06-09
Status: approved; implemented as a wake kind (see the event-wakes spec).

## Why this is a wake kind, not its own feature

The original design was a standalone "change-watcher": poll a URL, ping the
owner when its content changes. That is the event-wakes pattern with a remote
source instead of a local file. Running two watcher subsystems (one for files,
one for URLs) would duplicate the tick wiring, the inbox fold-back, the
cooldown/once/state machinery, and the doctor validation. So the URL watcher
is merged into `iris/wakes.py` as two new kinds, sharing all of that.

## The two kinds

- `url` — fires when the fetched body **changes** from the last fetch
  (edge-triggered, like `file_changed`). The first fetch arms without firing.
- `url_pattern` — fires when the fetched body **contains** the regex
  `pattern` and did not on the previous fetch (edge-triggered, so a page that
  keeps the matching text does not re-fire every tick).

Rule fields (in addition to the common `name`, `message`, `channel_id`,
`cooldown_secs`, `once`):

- `url` — the http/https URL to fetch (replaces `path` for these kinds;
  `path` is not used).
- `pattern` — required for `url_pattern`, a regex matched against the body.

## Fetching

The tick fetches with a bounded `urllib` GET: `IRIS_WAKE_HTTP_TIMEOUT`
seconds (default 15), at most 1 MB of body read, a plain User-Agent. The
comparison value is a SHA-256 of the body bytes (cheap, stable, and never
stores the page itself in state). A fetch that fails (network down, non-2xx,
timeout) is logged and treated as "no observation this tick": it does not
fire, does not change the stored digest, and does not crash the tick or the
other rules.

The fetcher is an injected callable so tests never touch the network, exactly
like the Discord `send` seam.

## What stays identical to file wakes

- The tick (`tick_wakes`) evaluates every rule, files and URLs alike, under
  the one wakes flock; one poisoned rule never aborts the others.
- A fire delivers the owner-authored `message` (plus, for `url_pattern`, the
  first matching line) as a Discord ping and an inbox fold-back note, queued
  exactly once, with the same failed-ping retry.
- `cooldown_secs`, `once`, state pruning, corrupt-state recovery, and
  `iris doctor` validation all apply unchanged.

## The one relaxed invariant, and the one that is not

The file-wake spec said "no network besides the ping." URL kinds necessarily
do a network GET, so that line is relaxed *for these kinds only*, and the
README/.env say so. The load-bearing invariant is untouched: **no model call
ever fires from the tick.** A URL wake is HTTP plus a hash, never inference.

## Config

- `IRIS_WAKE_HTTP_TIMEOUT` (float, default 15) — per-fetch timeout.

## Out of scope

- Authenticated fetches, POST, headers per rule (owner can front a URL with
  whatever they like; v1 is a plain GET).
- JSON-path / CSS-selector extraction (a regex on the body covers the
  "did the price/status change" cases; structured extraction can come later).
