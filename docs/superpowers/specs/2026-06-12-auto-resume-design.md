# Autonomous resume: owner-initiated chains may self-continue

## Problem

A background command (`run_in_background`) runs detached. When it finishes it
folds a note into the inbox and pings the home channel, but Iris does not act on
the result until the owner sends the next message. For a chain — "build the
videos, then schedule the uploads" — the owner has to poke Iris after every
step. He asked for the finished step to carry itself to the next one and report
back, without him in the loop.

This deliberately relaxes the **zero-idle-inference** invariant. Today the only
thing the clock may start is a pre-recorded scheduled job. Autonomous resume
adds a second bounded exception.

## The invariant, restated

The clock / a finished background task may start a model turn only when **the
owner initiated the work** and **opted in**. It may never start a conversation
from nothing, and never compose new intent at fire time.

A resume request exists only because the owner launched the task (through Iris,
with `autoresume=True`). It is bounded four ways:

1. **Off by default.** The whole path is inert unless `IRIS_AUTO_RESUME=true`.
2. **Per-launch opt-in.** Only a `run_in_background(autoresume=True)` call
   enqueues a resume. A plain background command behaves exactly as before
   (ping + fold, no turn).
3. **Daily cap.** `IRIS_AUTO_RESUME_MAX_PER_DAY` (default 12) bounds how many
   autonomous turns can fire in a UTC day — the runaway-chain backstop.
4. **Credit park.** When the credit guard says the month is nearly spent, a
   resume is dropped, not fired.

When a resume is dropped (off / parked / over cap), nothing is lost: the
ordinary fold note is already in the inbox, so the owner's next message still
surfaces the result.

## Why the resume runs in the bot process

`sessions.py` warns that two processes against one session store corrupt the
`claude` session. A resume that *continues the conversation* must therefore run
where the live session is — the bot process — serialized through the same
per-conversation runner as a normal message. A detached `iris watch` (separate
process) must not call `claude --resume` on the home session itself.

So the producer and consumer are split by a file-backed queue:

- **Producer** — `iris watch --resume` (spawned by `run_in_background`). On
  completion it enqueues `{conversation_id, prompt, ts}` to the resume queue,
  in addition to its existing fold note. The target conversation is the home
  channel (`discord:{IRIS_DISCORD_HOME_CHANNEL}`), consistent with where jobs
  already ping. It enqueues only when `IRIS_AUTO_RESUME` is on.
- **Consumer** — a background poll loop in the Discord bot. Every
  `IRIS_RESUME_POLL_SECS` it drains the queue, applies park + daily cap, and
  submits each accepted request as a synthetic `Turn` to that conversation's
  runner. The runner resumes the session and delivers the reply to Discord
  exactly as it does for a typed message.

The queue is file-backed (flock + atomic replace, capped like the inbox), so it
survives a bot restart and a producer that fires while the bot is down.

## Components

- `iris/autoresume.py` — `ResumeQueue` (enqueue/drain, cap), `ResumeBudget`
  (atomic per-UTC-day take-or-refuse), `dispatch_resumes(queue, budget, *, now,
  parked, submit)` (the pure consumer decision: drain, gate, hand each accepted
  request to `submit(conversation_id, prompt)`).
- `iris/notify/watch_cmd.py` — `watch(..., resume=False)`: enqueue on completion
  when `resume and config.auto_resume`.
- `iris/cli.py` — `watch --resume` flag.
- `iris/mcp/jobs.py` — `run_in_background(..., autoresume=False)`: append
  `--resume` to the watch argv; honest return text when the master flag is off.
- `iris/discord_adapter.py` — the poll loop + a `Turn` submit into the existing
  runner; `parse_conversation_channel()` pure helper for the channel id.
- `iris/config.py` — `auto_resume`, `auto_resume_max_per_day`,
  `resume_queue_file`, `resume_state_file`, `resume_poll_secs`.

## Out of scope

`start_job` / scheduled-job completions chaining (the producer is wired only
into `run_in_background`, the case the owner hit); resuming a launching *thread*
rather than the home channel (the jobs layer speaks no channel ids by design).
