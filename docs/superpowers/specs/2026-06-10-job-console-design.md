# Job console

Date: 2026-06-10
Status: BLOCKED — waiting on owner answers to the seven questions below.
Do not build any part of this until they are answered.

## Problem

Jobs run in the background and report over Discord, but the owner sometimes
sits at the terminal where Iris runs. A console would show jobs (and parked
ones, and artifacts) without asking the bot, and could offer actions like
cancel and resume.

## What exists already

- `iris-jobs.json` is the registry; everything a console needs to *read* is
  in it.
- The MCP tools (`list_jobs`, `job_status`, `cancel_job`, `resume_job`)
  already give the model the same capabilities in chat.

## Open questions (owner)

1. **Surface:** a full-screen TUI (textual, like `iris tui`) or a plain
   `iris jobs` table command — or both, table first?
2. **Actions:** read-only, or with cancel / resume / re-run from the console?
   Re-run in particular creates a new model call from a keyboard, which is
   fine (owner-driven) but should be deliberate.
3. **Live tail:** should the console tail a running job's progress? That
   requires the runner to stream partial output somewhere (today only the
   final report is recorded).
4. **Artifacts:** browse and re-deliver past artifacts from the console, or
   is the Discord upload at completion enough?
5. **Concurrent access:** the console would read (and with actions, write)
   the registry while the bot process and runners hold it. The flock pattern
   covers correctness, but should the console refuse actions while a runner
   is mid-state-change, or retry?
6. **Retention:** jobs accumulate in the registry forever right now. Should
   the console own pruning (e.g. `iris jobs prune --keep 50`), or should the
   store auto-prune terminal jobs past a cap?
7. **Scope:** same host only (read the local file), or eventually remote
   (a remote box) — which would mean an HTTP surface and auth that this
   design deliberately avoids today?

## Sketch (to be revised by the answers)

`iris jobs` lists; `iris jobs show <id>` details + report; actions and TUI
per answers. No new model-call paths. No new network listeners.
