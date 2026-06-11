# Hybrid job coordinator

Date: 2026-06-08
Status: approved

## Problem

Chat turns are synchronous and short. Real work (refactor a repo, research a
topic, batch-process files) takes minutes to hours and should not hold a
Discord reply hostage. Iris needs background jobs: the owner asks for
something big in chat, the agent kicks it off, the result comes back later.

The constraints that shape everything:

- **Single-user.** Jobs belong to the one owner. No queues per user, no
  fan-out to strangers.
- **Zero idle inference.** No model call may ever fire from a clock or a
  poll. A job's `claude -p` call is event-driven: it traces back to an
  explicit owner request. Nothing re-launches by itself.
- **Official CLI only.** Jobs run through `ClaudeDriver` and `_child_env`,
  the same hardened path as chat. No second integration.
- **The model never names paths or channels.** It refers to workspaces by
  owner-registered names and to jobs by recorded ids.
- **The chat driver's denylist stays untouched.** Jobs get a *different*
  denylist, derived from the same source of truth.

## Design: hybrid

Two tiers of concurrency, used together:

1. **Background jobs** are whole `claude -p` processes, launched detached
   from the chat turn that requested them. Each runs with its own session,
   its own (wider) tool grants, and a long timeout.
2. **Task subagents inside jobs only.** The chat denylist denies `Task` (and
   its `Agent` alias). The job denylist re-allows them, so a job can fan out
   into parallel subagents internally. Chat stays locked down; depth lives in
   jobs.

This is the hybrid: chat = one shallow locked-down turn; job = one deep
process that may fan out internally.

## Everything is behind IRIS_JOBS

`IRIS_JOBS` (bool, default off). When off: the jobs MCP tools answer "jobs
are disabled", `iris job-run` refuses, nothing spawns. Docs tell the owner
exactly what turning it on means.

## Components

### JobStore (`iris/jobs.py`)

File-backed registry, `IRIS_JOBS_FILE` (default `iris-jobs.json`). Same
pattern as `ReminderStore`: `fcntl` lock sidecar + tempfile + `os.replace`.

Job record:

```json
{
  "id": 3,
  "title": "audit the repo",
  "instructions": "...full prompt for the job...",
  "grants": ["files"],
  "workspace": "myrepo",
  "state": "pending|running|done|failed|cancelled|parked",
  "created_ts": 0.0, "started_ts": 0.0, "finished_ts": 0.0,
  "pid": 12345,
  "report": "...final text...",
  "error": null,
  "artifacts": ["clips/out.mp4"],
  "report_delivered": false,
  "channel_id": "recorded-home-channel-id"
}
```

States: `pending` (recorded, runner not yet started), `running`, `done`,
`failed`, `cancelled`, `parked` (credit guard refused the launch; see the
credit-guard spec). Terminal states keep the report for fold-back.

### Grants and the derived denylist

`DANGEROUS_BUILTINS` in `iris/driver.py` is the single source of truth for
what is dangerous. **An explicit `disallowed_tools` REPLACES the driver's
default denylist** (`_effective_disallowed`), so any explicit list must be
*derived* from `DANGEROUS_BUILTINS`, never hand-written, or a future addition
to the default silently never applies to jobs.

Grant names map to dangerous built-ins they unlock:

- `subagents` -> `Task`, `Agent` (and their helpers) — **always granted to
  jobs**; that is the point of a job.
- `shell` -> `Bash`, `BashOutput`, `KillShell`
- `files` -> `Write`, `Edit`, `NotebookEdit`

`job_disallowed(grants)` returns
`tuple(t for t in DANGEROUS_BUILTINS if t not in unlocked(grants))`.
Granted tools are also added to the job's `--allowedTools` so they are
pre-approved under permission mode `default` (deny outranks allow, so the
remaining denylist still binds).

`IRIS_JOB_GRANTS` is the **ceiling**: a comma list of grants the owner allows
jobs to ever receive (default empty = subagents only). A job request asking
for more than the ceiling is clamped to the ceiling, and the clamp is
reported in the start_job reply so the model can tell the owner.

### Job runner (`iris job-run <id>`, internal)

A small supervisor process, spawned detached (`start_new_session=True`) by
the MCP tool. It:

1. Loads the job, flips it to `running` (recording its pid).
2. Builds a `ClaudeDriver` (same `claude_bin`, persona optional via
   `IRIS_JOB_PERSONA`, model from `IRIS_JOB_MODEL` falling back to
   `IRIS_MODEL`) with the derived denylist, granted allowed-tools, the
   workspace as `--add-dir` (if any), and `IRIS_JOB_TIMEOUT` (default 1800 s).
3. Runs the instructions as one `claude -p` turn through `ClaudeDriver.run`.
4. Records `done`/`failed` + report text, collects `ARTIFACT:` files (see the
   workspaces spec), pings the owner's recorded home channel over plain
   Discord REST (no model call), and queues the report for fold-back.

The runner never calls the model except for the job turn itself. If it
crashes, the job record says `running` with a dead pid; `start_job` and
`list_jobs` repair obviously-dead `running` entries to `failed` when their
pid is gone (no poller — repair happens on the next owner-driven touch).

### MCP tools (`iris/mcp/jobs.py`)

FastMCP server `iris-jobs`, allowlisted as `mcp__jobs__*`:

- `start_job(title, instructions, grants="", workspace="")` — records the
  job, enforces the grants ceiling, resolves `workspace` (a registered name,
  never a path), checks the credit guard (may park), checks the concurrency
  cap `IRIS_JOBS_MAX` (default 2; over-cap jobs are recorded `pending` and
  reported as queued — the owner starts them later; nothing auto-launches),
  then spawns the detached runner.
- `job_status(job_id)` — state, timestamps, error, artifact names.
- `list_jobs(limit=10)` — newest first, one line each.
- `cancel_job(job_id)` — kills the runner's process group if running, flips
  to `cancelled`.
- `resume_job(job_id)` — relaunches a `parked` or `pending` job (an explicit
  owner-driven event, so it satisfies zero-idle-inference).

The tools never accept or return filesystem paths or raw channel ids; the
fold-back channel is the recorded `IRIS_DISCORD_HOME_CHANNEL`.

### Fold-back delivery

Two halves, neither calling the model:

1. **Immediate ping** — the runner posts "job #3 (audit the repo)
   finished" + the report head to the home channel via Discord REST, exactly
   like reminders delivery.
2. **Context fold-back** — the report is queued in the shared inbox
   (`iris/inbox.py`, `IRIS_INBOX_FILE`, default `iris-inbox.json`). On the
   owner's next chat message, `Agent.respond` drains the inbox under the
   conversation lock and prepends the entries to the prompt as
   `[while you were away] ...` so the brain knows the outcome. If the turn
   errors, the drained entries are restored so a flaky turn cannot eat a
   report.

### Inbox (`iris/inbox.py`)

Tiny shared queue: `append(text)`, `drain() -> list[str]`, `restore(items)`.
File-backed with the same flock + atomic-replace pattern. Used by jobs now,
wakes later. Capped (drop-oldest at 50 entries) so a runaway producer cannot
grow the prompt without bound.

## Out of scope (deliberately)

- A job console / TUI (separate spec, blocked on owner answers).
- Recurring or scheduled jobs (reminders + wakes cover the trigger side).
- Multi-turn jobs (one `claude -p` turn per job; subagents give the depth).
