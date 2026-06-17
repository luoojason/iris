# Proactive reviews: assist + maintain

## Goal

Make Iris a proactive assistant: on a clock, review state and either do high-value
work or improve herself, without the owner asking each time. Two cadences:

- **assist** (twice a day) — outward. Find the single highest-value thing for Jason
  and do it if small/reversible, ask if big/outward-facing, schedule if it needs time.
- **maintain** (every 3 days) — inward. Scan the wiki, memory, skills, and recent
  outcomes; do reversible housekeeping itself; propose destructive cleanups and any
  change to its own skills/standing-orders for approval.

This is the third deliberate relaxation of zero-idle-inference (after scheduled jobs
and autonomous resume): the clock starts an owner-enabled review. It is bounded
hard, see the leash.

## The leash (iris/proactive.py)

A review runs only when ALL hold:
1. `IRIS_PROACTIVE=true` (off by default; the whole path is inert otherwise).
2. The credit guard is not parked (hard backstop).
3. Real weekly usage is under `proactive_usage_max` (default 80%).

(3) is the load-bearing leash and uses the REAL account number, not a dollar guess.
`GET https://api.anthropic.com/api/oauth/usage` (Bearer token from
`~/.claude/.credentials.json`, header `anthropic-beta: oauth-2025-04-20`) returns
`seven_day.utilization` (0-100), the same number `/usage` shows. Because both Jason's
Mac and Iris share the one Max account, gating at 80% automatically preserves the top
20% as headroom for his interactive work.

The endpoint 429s under tight polling, so `UsageCache` refreshes at most every ~15
minutes and the gate reads the cache; a failed refetch keeps the last value; an
unknown value fails safe to "do not run". The OAuth token rotates/expires, so it is
re-read from the credentials file every cycle, never held.

## Flow (per tick)

`iris proactive-tick <assist|maintain>` (cron):
1. Gate (above). If blocked, exit with a one-word status, no model call, no spend.
2. Run ONE model turn via `Agent.respond("proactive:<kind>", PROMPT)` in a dedicated,
   continuous session per kind (so each cadence remembers its prior runs and does not
   repeat itself). The turn has Iris's full chat toolset, so it can act (write memory,
   edit the wiki, schedule jobs) and returns a summary.
3. If the reply is empty or exactly `NOTHING`, stay silent. Otherwise post it to the
   home channel as `[proactive: <kind>] ...`.

## Safety model (matches the owner's rule)

- Small and reversible → done itself (organizing, drafting, research, lessons to memory,
  fixing the wiki index/log, merging duplicates).
- Big or outward-facing (posts publicly, spends money, messages others, deletes wiki
  pages or memories) → described and asked, never done silently.
- Self-modification (rewriting its own skills/standing-orders) → proposed for approval,
  never silent. Changing its own behavior is the highest-stakes action and stays gated.

## Deploy

Off by default. To enable on the live bot: `IRIS_PROACTIVE=true` plus two irisbot cron
entries (assist twice daily, maintain every ~3 days) running
`python -m iris proactive-tick <kind>`. Set `IRIS_USAGE_BUDGET_USD` so the credit-guard
park backstop is meaningful.

## Out of scope

Reading the 5-hour window for the gate (weekly is the right horizon); a separate
per-model (Opus) sub-cap gate (add if Opus-heavy proactive work shows up).
