# Credit guard

Date: 2026-06-08
Status: approved

## Problem

Iris draws from the plan's monthly agent credit. Nothing in the codebase
knows how much has been drawn, so the owner finds out the credit is gone when
turns start failing. The guard makes the draw visible and applies gentle
brakes long before that.

It never blocks chat. The owner can always talk to their agent; the guard
slows the *expensive* things (background jobs, strong-model routing of
trivial turns) and tells the owner what is happening.

## Ledger (`iris/usage.py`)

A month-keyed JSON ledger, `IRIS_USAGE_FILE` (default `iris-usage.json`),
flock + atomic-replace like every other store.

```json
{
  "2026-06": {
    "cost_usd": 12.34,
    "turns": 412,
    "tokens": 9182734,
    "by_source": {"chat": 11.0, "job": 1.2, "notify": 0.14},
    "pinged": {"50": 1760000000.0}
  }
}
```

`record_turn(path, source, result)` adds a `ClaudeResult`'s `cost_usd` /
`context_tokens` to the current month. Fail-soft: recording must never break
a turn (same contract as `metrics.emit_turn`). Called from `Agent.respond`
and `LiveTurn._resolve` (source `chat`), the job runner (`job`), and the
watch notifier (`notify`).

`summary(path, budget, now)` returns the month's totals, the budget, percent
used, and the level (below). `month_key(now)` is UTC `YYYY-MM`.

## Budget and levels

`IRIS_USAGE_BUDGET_USD` (float, default 0 = guard off; everything records
but nothing pings, parks, or tightens). The `cost_usd` the CLI reports is
Anthropic's own per-turn estimate, summed; it is a proxy for credit draw, and
the docs say so plainly.

Levels, computed from percent-of-budget:

- `ok` (< tighten)
- `tighten` (>= `IRIS_USAGE_TIGHTEN_AT`, default 80)
- `park` (>= `IRIS_USAGE_PARK_AT`, default 95)

## The three brakes

1. **Tick threshold pings.** `budget_tick(config)` runs inside
   `iris reminders-tick`, beside reminder delivery. For each threshold in
   `IRIS_USAGE_PING_AT` (default `50,80,95`) that the month's percentage has
   crossed and not yet pinged, it sends one plain Discord REST message to the
   home channel and records the ping in the ledger. No model call, ever; a
   tick with nothing crossed sends nothing.
2. **Job parking.** At `park` level, `start_job` records the job as `parked`
   instead of launching, and says so. Parked jobs stay parked until the owner
   explicitly resumes them (`resume_job`); the tick never launches anything.
3. **Light-model tightening.** At `tighten` level and above, the router gets
   more aggressive about the light model: `Agent` multiplies
   `trivial_max_chars` by `IRIS_TIGHTEN_FACTOR` (default 3) when routing.
   Heavy-hint and attachment gates still force the strong model — quality on
   hard turns is never sacrificed, only the cheap chatter gets cheaper. The
   level is read through a small mtime-cached reader so routing adds no
   meaningful I/O per turn.

## Surfaces

- **`iris usage` CLI** — prints the month, totals by source, budget,
  percentage, level, and which pings have fired. Exit 0 always (it is a
  report, not a check).
- **Usage MCP tool** (`iris/mcp/usage.py`, server `iris-usage`, tool
  `usage_report`) — the same summary as text, so the *agent* can answer
  "how much credit have I burned?" without shell access.
- **doctor** — prints budget/level when a budget is set.

## Invariants

- No model call on a clock: the tick only reads the ledger and POSTs text.
- Recording is fail-soft and adds one locked read-modify-write per turn.
- With no budget set, behavior is identical to before this feature except
  that the ledger file grows.
