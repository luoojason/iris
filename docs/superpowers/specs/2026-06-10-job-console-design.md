# Job console

Date: 2026-06-10 (answers recorded 2026-06-11)
Status: approved — the seven questions are answered; build it.

## Problem

Jobs run in the background and report over Discord, but the owner sometimes
sits at the terminal where Iris runs. A console shows jobs (and parked ones,
and artifacts) without asking the bot, and lets the owner act on them — and,
per the owner's decision, kick off jobs directly from the keyboard too.

## What exists already

- `iris-jobs.json` is the registry; everything a console needs to *read* is
  in it (`iris/jobs.py` `JobStore`).
- The MCP tools (`list_jobs`, `job_status`, `cancel_job`, `resume_job`) give
  the model the same capabilities in chat. The console is the no-model,
  no-Discord, no-credit equivalent for when the owner is at the box.

## The owner's answers

1. **Surface — both.** A plain `iris jobs` table command *and* a full-screen
   TUI (textual, like `iris tui`). Table is the testable core; the TUI is a
   thin view over the same functions.
2. **Actions — all of them, plus job creation.** cancel / resume / re-run,
   and a hand-authored `iris jobs run` that creates and launches a job from
   the CLI with no model. Owner-authored, so the grants are still clamped to
   the `IRIS_JOB_GRANTS` ceiling (one policy for model- and owner-created
   jobs; clamps are reported). Gated on `IRIS_JOBS`.
3. **Live tail — no, not for now.** Only the final report is shown. No
   streaming of partial output; the runner is unchanged.
4. **Artifacts — console too.** Browse a job's artifacts and re-deliver them
   to the Discord home channel from the console (the one place the console
   touches Discord — a single deliberate re-upload action, not a chat
   surface).
5. **Concurrent access — refuse, do not retry.** Console actions go through
   the store's atomic `transition()` with the expected `from_states`. If a
   runner moved the job underneath, the transition returns `None` and the
   console reports the refusal with the job's current state. It never forces
   or busy-retries.
6. **Retention — auto-prune.** The store auto-prunes terminal jobs
   (done/failed/cancelled) past a cap (`IRIS_JOBS_KEEP`, default 50) on every
   `add`, keeping the most recent. Active jobs (pending/running/parked) are
   never pruned. `iris jobs prune [--keep N]` exposes a manual sweep too.
7. **Scope — local host only.** No HTTP surface, no auth, no remote. Discord
   already covers "check from my phone." The console reads the local file.

## Commands

`iris jobs` (alias `iris jobs list`) — table: id, state, title, age/finished,
grants, workspace. `iris jobs show <id>` — full detail incl. instructions,
report, error, artifacts. `iris jobs run --title T --instructions I
[--grant g1,g2] [--workspace name]` — create + launch (ceiling-clamped, gated
on IRIS_JOBS). `iris jobs cancel|resume|rerun <id>` — actions (refuse on a
lost race). `iris jobs artifacts <id>` — list artifact names. `iris jobs
deliver <id>` — re-upload artifacts to the home channel. `iris jobs prune
[--keep N]` — manual prune. `iris jobs --tui` — the full-screen view.

## Invariants

- No new model-call path. `iris jobs run`/`rerun` spawn a detached runner
  exactly like the MCP `start_job`; the runner makes the one model call, the
  console makes none.
- Every launch path (run, rerun, TUI re-run) funnels through one gated launch
  that re-clamps grants to the *current* `IRIS_JOB_GRANTS` ceiling, applies
  the credit-guard park, and honors the `jobs_max` admission cap. A re-run of
  an old job can never resurrect a grant the owner has since revoked.
- No new network listener. The only outbound is the artifact re-delivery
  (the existing Discord REST upload).
- Actions are atomic-or-refused, never force.
