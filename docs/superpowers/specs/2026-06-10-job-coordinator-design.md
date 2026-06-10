# Job Coordinator (hybrid delegation) — design

Date: 2026-06-10
Status: approved direction (hybrid), built from the merge-server tree (HEAD 576e08d, 208 tests)

## Context

Iris answers one message with one `claude -p` turn. Big asks therefore mean one long
turn that pins the conversation lock (up to 1800s on the live path) and delivers
nothing until the end. The owner wants delegation: Iris breaks a big ask into pieces,
hands each piece to a worker, keeps chatting, and surfaces results as they land.

Decision (owner-approved): hybrid architecture.

1. A Python-layer jobs system spawns tracked background `claude -p` runs (fresh
   sessions, own timeouts, per-job tool policy) and reports through the notify spine.
2. Native Task subagents stay denied in chat turns but are re-enabled inside
   background jobs, so a single job can fan out internally.

Invariants that must survive: single-user only; zero idle inference (no model call on
a clock, ever); official CLI only (every run goes through ClaudeDriver/_child_env).

## Components

### 1. JobStore (`iris/jobs.py`)

File-backed registry, clone of ReminderStore's shape (fcntl sidecar lock, atomic
tempfile+os.replace writes, corrupt-tolerant load, max(ids)+1): the MCP server
subprocess and the bot process share it safely, and it survives the turn-timeout
group SIGKILL because every state change hits disk before returning.

Record:

```json
{
  "id": 3,
  "title": "refactor parser",
  "prompt": "...",
  "status": "pending | running | done | failed | cancelled | interrupted",
  "created_at": 0.0, "started_at": null, "finished_at": null,
  "channel_id": "", "conversation_id": "",
  "model": "", "timeout_s": 1800, "grants": ["Task"],
  "cancel_requested": false,
  "result": {"text": "...", "session_id": "...", "is_error": false, "error": null,
             "cost_usd": 0.0, "duration_ms": 0, "context_tokens": 0}
}
```

API: `add(prompt, title, *, model="", timeout_s=None, grants=None, channel_id="",
conversation_id="") -> int`; `get(id)`; `all(status=None)`; `update(id, **fields) ->
bool`; `claim_pending(limit) -> list[dict]` (atomic pending->running flip, the
pop_due analog); `request_cancel(id) -> str` (pending -> cancelled outright; running
-> cancel_requested=true; terminal -> message). Default path `iris-jobs.json`
(IRIS_JOBS_FILE), next to the session store.

### 2. Per-job driver policy (`iris/jobs.py`)

`build_job_driver(base_driver, job, config) -> ClaudeDriver` via
`dataclasses.replace`: per-job `timeout` (timeout_s), `model` override, and a
COMPUTED denylist: `tuple(t for t in DANGEROUS_BUILTINS if t not in granted)`.
Granted = job.grants intersected with the operator ceiling `IRIS_JOB_GRANTS`
(default `Task`). Explicit disallowed_tools takes full control of the denylist, so
it is always derived from DANGEROUS_BUILTINS, never hand-listed. The chat driver is
never mutated; a test pins that the interactive denylist still contains Task.
Jobs get `append_system_prompt` = a short JOB_PREAMBLE (you are a background worker,
work autonomously, your final message is the report delivered to the owner).

### 3. StreamTurn.cancel() (`iris/stream_driver.py`)

Jobs run through StreamDriver (per-job instance wrapping the job driver, idle/total
timeouts from config/job) so each job has a killable process handle and the existing
hardened watchdog. New public `StreamTurn.cancel()`: kill the process group via the
existing `ClaudeDriver._kill_tree` path and mark the turn finished; no third kill
path is invented. Fresh sessions only (`start(prompt, None, model)`), so no
heal/resume logic is needed.

### 4. JobRunner (`iris/jobs.py`)

Bot-side lifecycle owner. Threaded, mirroring Agent's discipline (daemon threads,
joinable handles exposed for tests, `sync=True` escape hatch like compact_async).

- Discovery: a watcher thread stat()s the registry file every
  IRIS_JOB_POLL_SECONDS (default 2.0) and re-reads only on mtime change; plus
  `turn_finished(cid)` nudges an immediate check. Pure file I/O, zero model calls.
- Spawn: claim_pending up to free slots (threading.Semaphore, IRIS_JOB_CONCURRENCY
  default 2 to respect Max rate limits), one worker thread per job:
  StreamDriver(build_job_driver(...)).start(prompt, None, model) ->
  wait_primary/wait_finished -> write result + status, emit_turn with
  conversation_id `job:<id>` (metrics for free).
- Cancel: the watcher checks cancel_requested on running jobs it owns ->
  StreamTurn.cancel() -> status cancelled.
- Restart recovery: on start(), any job left `running` with no live handle ->
  status interrupted, ping owner.
- Attribution: MCP tools cannot see the calling conversation. The adapter calls
  `turn_started(cid)` / `turn_finished(cid)`; a new pending job created inside
  exactly one active turn window is stamped with that cid + channel. Ambiguous
  (overlapping windows) stays unstamped and falls back to the notify channel.

### 5. Delivery (fold-back first, spine fallback)

- Fold-back (preferred, Discord v1): JobRunner calls an adapter-registered
  `deliver(channel_id, conversation_id, text) -> bool` callback. The adapter
  resolves the runner AT DELIVERY TIME from its runners dict (stale-runner trap
  after !reset) and marshals `runner.submit(Turn(text))` onto the event loop with
  `call_soon_threadsafe`. Text shape:
  `[background job #3 "title" finished]\n<result text>` — the conversation turn
  that consumes it is the one model call, lets Iris relay in voice and synthesize
  multiple completions (the runner's coalescing batches near-simultaneous ones),
  and keeps her session aware of what landed. Live turns get it injected; idle
  conversations get it as the next turn.
- Spine fallback (no callback, unstamped job, delivery returns False, or
  Telegram/TUI): Event(source="job", kind="finished"/"failed") -> gate ->
  compose -> deliver.send to the job's channel_id or config.notify_channel.
  compose._template grows kind/source-aware branches ("job done: <title> in 12m" /
  "job failed: ..."), and a job-flavored failure prompt variant beside
  _failure_prompt; the one-shot triage model call still happens only on failure,
  template fallback discipline unchanged. The reserved "fold" verdict is not
  touched. No started/needs-input events in v1 (spawn_job's return string is the
  start receipt).
- Never both: fold-back delivered means no spine ping for that job.

### 6. MCP jobs server (`iris/mcp/jobs_server.py`)

FastMCP("iris-jobs"), config key `jobs`, registry writer ONLY (the bot is the only
spawner; the server may be SIGKILLed mid-call at any time). Conventions copied from
reminders/memory servers: guarded import, module-level `STORE =
JobStore(os.environ.get("IRIS_JOBS_FILE", "iris-jobs.json"))`, primitive args,
Args docstrings, friendly-string returns, never raises.

Tools (allowlist: `mcp__jobs__spawn_job, mcp__jobs__list_jobs, mcp__jobs__job_status,
mcp__jobs__cancel_job, mcp__jobs__job_result`):

- `spawn_job(prompt, title="", model="", timeout_minutes=0, grants="") -> "Job #N
  queued: <title>"`. grants is comma-separated builtin names, validated against
  DANGEROUS_BUILTINS names, recorded as requested (the ceiling is enforced
  bot-side at spawn). Near-duplicate guard: refuse an identical pending
  title+prompt within a few seconds (driver retry can re-run tool side effects).
- `list_jobs(status="")` -> "#3 [running 4m] refactor parser" lines.
- `job_status(job_id)` -> one-job detail or "No job #N.".
- `cancel_job(job_id)` -> store.request_cancel outcome string.
- `job_result(job_id, max_chars=4000)` -> clamped stored result text or error.

### 7. Wiring

- Config: IRIS_JOBS_FILE, IRIS_JOB_CONCURRENCY (2), IRIS_JOB_TIMEOUT (1800),
  IRIS_JOB_IDLE_TIMEOUT (300), IRIS_JOB_POLL_SECONDS (2.0), IRIS_JOB_MODEL (""),
  IRIS_JOB_GRANTS ("Task"). Same _split/_flag parser conventions.
- discord_adapter: build_client gains optional job_runner; on_message wraps the
  turn with turn_started/turn_finished; registers the deliver callback described
  above. conversation_id format stays `discord:<channel_id>`.
- cli: `iris jobs list|show <id>|cancel <id>|spawn <prompt...>` subcommand
  (operator/SSH surface); discord mode constructs JobRunner from config and
  starts/stops it around the client run.
- doctor: warn when the mcp config contains a `jobs` server but allowed_tools has
  no mcp__jobs__ entries (extends the silent-skip warning).
- Docs: README tools section, .env.example, examples/mcp.example.json `jobs` entry.

## Compliance

- Single-user: spawn surfaces are the owner-gated chat (should_handle/allowed user
  ids upstream) and the local CLI. Delivery targets are recorded channel ids or
  config.notify_channel, never a channel the model names at delivery time.
- Zero idle inference: watcher is stat-only; per finished job at most ONE model
  call (the fold-back conversation turn, or the failure triage one-shot). No
  polling model calls anywhere.
- Official CLI: every run is ClaudeDriver/StreamDriver through _child_env (IRIS_*
  and ANTHROPIC key scrubbing intact). Cost lands in metrics per job.
- Tool safety: ceiling env keeps Bash/Write/Edit denied for jobs unless the
  operator widens IRIS_JOB_GRANTS; chat denylist untouched and pinned by test.

## Testing

Conventions per the suite (no conftest, local fakes, no real claude/network):
test_jobs_store.py (ReminderStore-shaped: tmp_path, persistence across
re-instantiation, claim atomicity, cancel transitions); test_jobs_driver.py (pure
build_command argv assertions: Task absent only on granted job driver, ceiling
enforced, fresh-session `--resume` absent, chat driver pinned unchanged);
test_stream_cancel.py (FakeProcess, assert proc.killed); test_jobs_runner.py
(fake stream driver factory, sync mode, lifecycle pending->running->done/failed/
cancelled/timeout/interrupted, stamping windows, deliver-callback vs spine
fallback with sender= capture, semaphore cap with SlowDriver pattern);
test_jobs_server.py (importorskip mcp, monkeypatch STORE path, friendly strings,
duplicate-spawn guard); compose/template additions in test_notify_compose.py.

## Out of scope (v1)

Per-job cwd (driver has no cwd field yet); streaming job progress into chat;
Telegram/TUI fold-back; job-needs-input escalation events; morning-briefing "fold"
digests; multi-turn jobs resuming their session (result.session_id is stored, so a
v2 "continue job" can resume it).
