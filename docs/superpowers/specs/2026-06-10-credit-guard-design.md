# Credit guard and cost visibility — design

Date: 2026-06-10
Status: draft (backlog item 2, builds on the job coordinator)

## Context

The agent-credit pool activates 2026-06-15 (Pro $20 / Max5x $100 / Max20x $200 per
month, API-metered) and the coordinator multiplies concurrent `claude -p` runs. Iris
already records per-turn cost (iris/metrics.py JSONL: cost_usd, model, routed,
transport, conversation_id incl. `job:<id>`), but nothing reads it. A runaway
background job fleet is the most likely systemic failure once delegation ships.
Everything here is deterministic arithmetic over the metrics file: zero model calls.

## Components

### 1. `iris/budget.py` (pure)

- `read_metrics(path, since_ts) -> list[dict]`: tolerant JSONL reader (skip bad
  lines), no caching.
- `summarize(records) -> dict`: totals and breakdowns: cost by model, by transport
  (discord/telegram/tui/chat/job), error rate, turn count, top conversations by
  cost, context-token p95.
- `window(now, period) -> since_ts` for day/week/month (calendar month, local time).
- `projection(month_records, now) -> float`: linear month-end estimate.
- `thresholds_crossed(spent, credit, already_pinged: set[int]) -> list[int]`:
  which of 50/80/95 (%) newly crossed. Pure.
- `BudgetState(path)`: tiny JSON state file (month key, pinged thresholds,
  park_until ts), atomic writes like SessionStore._flush. Default path
  `iris-budget.json` (IRIS_BUDGET_STATE).

### 2. `iris usage` CLI

`iris usage [--period day|week|month] [--json]`: prints spend total, per-model and
per-transport lines, job spend separated, error rate, top 5 conversations, and,
when IRIS_MONTHLY_CREDIT is set, percent used + linear projection vs the credit.
Reads config.metrics_file; friendly message when unset or missing. No model calls.

### 3. Usage MCP tool (`iris/mcp/usage_server.py`)

FastMCP("iris-usage"), config key `usage`, READ-ONLY. One tool:
`usage_summary(period: str = "month") -> str` rendering the same summary as the
CLI (shared formatter in iris/budget.py). Env block needs IRIS_METRICS_FILE and
optionally IRIS_MONTHLY_CREDIT. Allowlist: `mcp__usage__usage_summary`.

### 4. Threshold pings on the tick

`iris reminders-tick` gains a budget check (only when IRIS_MONTHLY_CREDIT > 0 and
metrics file set): compute month spend, thresholds_crossed vs BudgetState, send a
TEMPLATED notify ping per newly crossed threshold ("budget: 80% of the monthly
agent credit used ($83.12 of $100; projecting $122 by month end)"), record in
state. Clock-driven, therefore template-only by rule: compose's model path is
never invoked from the tick. State is month-keyed so pings re-arm each month.

### 5. Job parking and near-cap tightening (JobRunner)

- Parking: when a finished job's error matches the driver's terminal
  credit/rate-limit markers (reuse the existing classifier exposure, do not
  re-invent string matching), JobRunner sets park_until = now +
  IRIS_BUDGET_PARK_MINUTES (default 60) in BudgetState, stops claiming new jobs
  while parked (pending jobs stay queued), and sends ONE templated ping ("jobs
  parked until ~HH:MM: the credit pool or rate limit pushed back"). The watcher
  resumes claiming after expiry with a templated "jobs resumed" ping.
- Tightening: when month spend >= 80% of IRIS_MONTHLY_CREDIT, new jobs without an
  explicit model pin run on config.light_model when set (chat routing untouched;
  jobs are where the volume is). A job that names its model is honored.

### 6. Config

IRIS_MONTHLY_CREDIT (float USD, 0/unset = guard off), IRIS_BUDGET_STATE
(iris-budget.json), IRIS_BUDGET_PARK_MINUTES (60). metrics_file already exists.

## Compliance

Zero idle inference: every piece is file arithmetic; tick pings are templates;
the MCP tool renders text. Single-user: nothing new outbound except notify-spine
pings to the configured channel. Official CLI: untouched.

## Testing

budget.py pure functions (synthetic JSONL incl. bad lines, month boundaries,
threshold re-arming across months); CLI via capsys + tmp metrics; MCP server via
monkeypatched module path fixtures; tick integration with a fake sender asserting
one ping per threshold and template-only (a driver factory that fails the test if
called); JobRunner parking (credit-error result -> no further claims while
parked -> resume after expiry) and tightening (light model used at >=80% unless
job pins a model) with the existing fake stream-driver seams.

## Out of scope

Hard spend caps (refusing chat turns), per-project budgets, the cc-dashboard
panel (reads the same JSONL already), routing changes to chat turns.
