# Job console — design (DRAFT: blocked on owner answers below)

Date: 2026-06-10
Status: draft from the research brief; v1 build waits for the owner's taste calls.

## Owner questions (answer these, then the build is mechanical)

1. **Board surface:** pinned dashboard message in a channel (v1 default, zero
   provisioning) or a dedicated forum channel with one post per job and status
   tags (cleaner, more setup, clean v2 swap either way)?
2. **Controls:** slash commands only, or also a single Cancel button on
   pending/running cards? The button is ~40 lines (DynamicItem, restart-safe by
   construction) but is the one piece that can silently dead-end if
   registration is ever missed.
3. **Channel:** reuse IRIS_NOTIFY_CHANNEL for cards+dashboard, or a dedicated
   IRIS_CONSOLE_CHANNEL? Dedicated keeps the ledger clean; reuse keeps one
   phone surface.
4. **Ping policy:** keep the spine ping alongside the card (default, slightly
   duplicative in-channel) or suppress it when the console is on?
5. **Threads:** result thread for every terminal job, or only done/failed?
   Auto-archive 1 day or 3?
6. **Resume grants:** OK that talk-to-the-job replies run with full chat-driver
   tool access (not the job's restricted ceiling)? The alternative forks the
   conversation machinery and loses dead-session healing.
7. **IRIS_GUILD_ID:** confirm the guild id for command sync; the console
   refuses to register commands when IRIS_ALLOWED_USER_IDS is empty.

## v1 in one paragraph

A deterministic, zero-model-call ledger over the job registry: one plain-text
card message per job, posted at first observation and edited in place on status
transitions only; a pinned dashboard message re-rendered from the whole
registry (throttled to 5s); guild-scoped slash commands (/jobs, /job,
/job-cancel, /job-result) on the existing plain discord.Client via an attached
CommandTree; and a result thread per finished job that doubles as
talk-to-the-job by seeding SessionStore with `discord:{thread_id} ->
result.session_id`, after which the existing thread auto-reply path resumes the
job's session as a normal new conversation (a model call only when the owner
actually replies). All behind IRIS_CONSOLE (default off).

## Components

- `iris/console.py` (new): pure renderers (render_card / render_dashboard /
  render_report_header; guaranteed non-empty, <=1800 chars) + a Console
  reconciler: fingerprint (status + cancel_requested + title hash) vs console
  fields stored on the job record (console_msg_id, console_fp,
  console_thread_id via JobStore.update); post on first sight, edit on
  mismatch, thread + chunked report at terminal, repost on 404. Throttles:
  2s/message, 1 edit/1.5s/channel drain, 5s dashboard. Dashboard msg id in a
  sidecar iris-console.json.
- `iris/discord_rest.py` (new): sync urllib helper beside
  reminders.send_discord_message: post_message (returns id), edit_message,
  create_thread_from_message, post_in_thread, pin_message; honors Retry-After
  once then defers to the next reconcile pass. Callable from runner threads,
  no gateway dependency, injectable for tests.
- JobRunner hooks (~6 lines): after claim, in _finish_job beside emit_turn, in
  _recover_interrupted, and console.reconcile() from the watcher poll (the only
  signal for MCP-subprocess writes: spawn/cancel land cross-process). Delivery
  flow untouched; the card is a parallel ledger, not a delivery path.
- `iris/console_commands.py` (new): CommandTree on a Client subclass,
  setup_hook syncs guild-scoped (seconds, vs 1h global; setup_hook not
  on_ready). Every interaction handler gates on allowed_user_ids FIRST:
  interactions bypass should_handle entirely (this is the v1 security
  footgun). Cancel button as DynamicItem `iris:job:cancel:<id>` if Q2 says yes.
- Talk-to-the-job: at terminal delivery with a session_id, create the thread
  and seed the store. Thread header states the honesty deltas: replies run on
  the chat driver (not the job ceiling), and an expired session silently
  restarts fresh.
- Config: IRIS_CONSOLE, IRIS_GUILD_ID, IRIS_CONSOLE_CHANNEL (falls back to
  IRIS_NOTIFY_CHANNEL). Startup probe-and-warn for missing perms (threads,
  pinning) and empty allowlist.

## Card shapes (examples)

```
🔵 #17 running — Summarize arxiv backlog
started 12:05 · 3m elapsed · model: default · grants: Task · timeout 30m
[Cancel]

🟢 #17 done — Summarize arxiv backlog
finished 12:18 · 13m · $0.42 · 18.3k ctx
> Read 14 papers; 3 are relevant to context degradation. Shortlist with notes…
Full report + talk to this job → thread on this message

IRIS JOBS — 2 running · 1 pending · 14 done · updated 12:18
🔵 #18 running 02:11 — Refactor stepscope funnels
🟡 #19 pending — Nightly metrics rollup
— recent —
🟢 #16 done 38m ago · $0.31
```

## Deferred from v1

Forum-channel board; heartbeat/elapsed-ticker edits (state transitions only);
buttons beyond Cancel (retry/park need registry statuses that do not exist);
file-attachment reports (chunk at 2000 chars, cap 5 chunks, footer points at
/job-result for more); embeds; any spine compose changes; reaction fallback;
Telegram parity.

## Risks the build must respect

One channel concentrates the create/edit rate bucket (cards + dashboard +
spine pings; restart recovery is the burst moment — reconcile drains slowly,
never bursts). Interactions bypass the message-path single-user gate; the
allowlist check in every handler is load-bearing. Cross-process edges arrive
only via the 2s mtime poll. Renderer must never emit empty content (Discord
400 50006). Fold-back jobs intentionally appear twice (conversation + ledger);
if that reads as noise the lever is Q4, not weakening the ledger.

## Testing

Renderer golden-text tests; reconcile state machine with fake REST sender +
injected clock (post/edit/404-repost/throttle); restart recovery (registry
ahead of cards); auth-gate tests for every handler; thread-seeding test
asserting SessionStore key and no model call until a reply arrives.
