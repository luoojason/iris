# Goal loop: a standing objective the clock advances until done

## Goal

Let the owner hand Iris an *objective* ("get the roadmap shipped", "find me three
viable suppliers"), not just a task, and have her carry it forward on her own —
one bounded work step at a time — until it is achieved or genuinely needs the
owner. This is the assistant behavior Jason asked for after seeing OpenClaw's
"keep working on it" feature: Iris should be able to decide a thing is worth more
work and schedule herself to do it, at higher quality than the reference.

This is the **fourth** deliberate relaxation of zero-idle-inference (after
scheduled jobs, autonomous resume, and the proactive reviews). The clock advances
owner-set goals. It never starts a conversation from nothing: a goal exists only
because the owner set one.

## The leash

A goal step runs only when ALL hold (the gate is shared verbatim with the
proactive reviews — `iris/proactive.py`):

1. `IRIS_GOALS=true` (off by default; the whole path is inert otherwise).
2. There is at least one `active` goal (no work, no usage fetch, no spend).
3. The credit guard is not parked (hard backstop).
4. Real weekly usage is under `proactive_usage_max` (default 80%) — read from the
   OAuth usage endpoint, cached ≤15 min, unknown fails safe to "do not run".

Beyond the gate, three loop-specific bounds:

- **Per-goal step budget** (`IRIS_GOALS_MAX_STEPS`, default 20; a goal may set its
  own). At budget the goal transitions `blocked` and asks the owner to extend,
  change approach, or drop it. A goal that never converges cannot burn forever.
- **Active-goal cap** (`IRIS_GOALS_MAX_ACTIVE`, default 10) on the `set_goal` chat
  tool, so a runaway turn or prompt injection cannot fill the loop with work.
- **One goal per tick**, least-recently-worked first (smallest `updated_ts`). Many
  goals rotate fairly; a single tick never fans out unbounded spend.

## Flow (per tick)

`iris goal-tick` (cron):
1. If `IRIS_GOALS` off → `disabled`. If no active goal → `idle`. (Both before any
   usage fetch or model call.)
2. Gate (above). If blocked → `skipped(util=…,parked=…)`, no model call, no spend.
3. Pick the least-recently-worked active goal. If it is at its step budget →
   transition `blocked`, ask the owner, return `budget`.
4. **Step**: one model turn on the goal's own session (`goal:<id>`), with Iris's
   full chat toolset, asked to do the single most useful next thing and report
   what changed / whether it's done / what it needs. A failed turn (rate limit,
   dead session) returns `step-error` and does **not** spend the budget — the next
   tick retries.
5. **Judge**: a separate, cheap-model (`IRIS_GOAL_JUDGE_MODEL`, default
   `claude-haiku-4-5`), tool-less, fresh-session check rules the report
   `DONE` / `BLOCKED` / `CONTINUE`. The worker cannot mark its own goal done; a
   skeptical second model must agree. An unrecognized reply defaults to
   `continue` (the budget still bounds it).
6a. **Verify (only on a `done` verdict).** The judge ruled on the worker's
   self-report, so a `done` is re-checked by an independent read-only turn
   (`IRIS_GOALS_VERIFY_DONE`, default on; cheap model, chat toolset, fresh
   `goal-verify:<id>` session) that inspects the actual work and replies
   `CONFIRMED` / `UNCONFIRMED`. Unconfirmed or an erroring verify downgrades the
   verdict to `blocked` and asks the owner, so a `done` the work doesn't back up
   is never accepted. It fires only on `done`, so it adds at most one cheap call
   per completion, not per step.
7. Record the step (steps+1, log entry, `updated_ts`). Then:
   - `done` → transition `done`, report to the origin thread, return `done`.
   - `blocked` → transition `blocked`, ask the owner, return `blocked`.
   - `continue` → stay active, **silent** (no Discord noise), return `advanced`.

## Fail-open, never wedge

If the judge errors (model unreachable), the tick treats it as `blocked` and asks
the owner rather than silently looping or claiming success. A broken credit ledger
degrades to "not parked" but the usage gate still applies. The store uses the same
flock + atomic-replace as the inbox/resume queue, so the chat process (which sets
goals) and the cron tick (which advances them) never tear the file.

## Routing

Each goal carries the `conversation_id` of the thread it was set in
(`IRIS_ORIGIN_CHANNEL`, plumbed by the driver). Done/blocked reports route there
via `_origin_channel`, falling back to the home channel — so an answer lands where
the owner asked, not always in a catch-all.

## Surfaces

- Chat: `set_goal(text, max_steps=?)`, `list_goals()`, `cancel_goal(id)` (MCP
  server `iris.mcp.goals`, registered in the example MCP config).
- Owner CLI: `iris goals` (list), `iris goals cancel <id>`, `iris goal-tick`
  (cron), and an `iris doctor` line reporting the loop's state.

## Deploy

Off by default. To enable on the live bot: `IRIS_GOALS=true` plus one irisbot
cron entry running `python -m iris goal-tick` (every few hours is plenty — the
usage gate, not the cadence, is the real bound). Set `IRIS_USAGE_BUDGET_USD` so
the credit-guard park backstop is meaningful, exactly as for the proactive reviews.

## Out of scope

A separate per-goal sub-budget on the *judge* model (it is cheap and one call per
step); concurrent multi-goal advancement in a single tick (deliberately one, for
spend safety); goal dependencies / chaining (a goal can schedule a job if it needs
one). Add if real use shows the need.
