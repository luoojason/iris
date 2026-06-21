# Comprehensive Autonomous Iris — Vision and Roadmap

**Date:** 2026-06-21
**Status:** Vision/decomposition for review (drives per-sub-project spec → plan cycles)
**Base line:** `origin/main` (the canonical, deployed line)

## Goal

Iris as a comprehensive agent with the capabilities and connections of Claude Code plus the Hermes agent, able to run completely autonomously and finish tasks end to end, behaving like an outgoing, proactive personal worker. Governing principle: **nothing is hard-coded or pre-set; the owner brings or creates their own MCP connections.**

## Reality check: most of this already exists (on `origin/main`)

An inventory of both branch lines shows the autonomous, proactive worker is roughly 90% built on `origin/main` (the line the live bot runs). What already ships there:

- **Autonomy loop:** `goals.py` (goal loop + `goal-tick`), `proactive.py` (`proactive-tick` review), `autoresume.py` (resume queue), `heartbeat.py` (health), `schedules.py`, `leash.py` (self-initiated-spend leash).
- **Task execution:** `jobs.py` (JobStore/JobRunner, background `claude -p` jobs, per-job driver policy, native Task subagents inside jobs), `workspaces.py` (owner-bound repo workspaces), `verify.py` (verification gate), `jobs_console.py` / `jobs_tui.py`.
- **Connections / tools (MCP servers):** memory, reminders, jobs, usage, wiki, goals, session_search (history), skills, discord, tts, youtube, approvals, publish.
- **Hermes port (PR #12):** `audit.py` (model-free security self-audit), `gating.py` (clock-context tool gating), `digest.py` (session recap), `approvals.py` + `mcp/approvals_server.py` (Discord Approve/Deny, fail-closed).
- **Memory + state:** conversation-scoped memory, `statefile.py` shared JSON stores, `trace.py` ledger, `inbox.py` fold-back.
- **Transport:** Discord, Telegram, TUI, terminal chat, live-interrupt (stream-json), `commands.py` bang-command control plane, `webhooks.py` inbound wakes, `wakes.py` event wakes.
- **Safety:** denylist of dangerous built-ins, `--strict-mcp-config`, secret stripping, allowlist as the execution boundary, credit guard / job parking, single-user gate, approvals.

So the project is **not** a greenfield build. It is consolidation, one genuinely missing capability, and a proactivity/personality layer.

## The divergence problem (must resolve first)

There are two diverged lines that never merged, from common ancestor `15ea965`:

- **`origin/main`** — canonical, deployed, ahead by ~52 commits. Everything above lives here.
- **`buffer-publishing`** (local) — a **stale fork**, behind main, whose only unique value is `iris/buffer.py` (Buffer publishing) and `iris/budget.py`. PR #13 was built on this stale base, and it hard-codes Buffer, which violates the new principle.

Resolution: treat `origin/main` as canonical. Do **not** merge PR #13 as-is. Carry forward only the *insight* from the Buffer work (publishing should be a pluggable connection, not a native client) into the connections foundation below, then retire the stale branch and close PR #13.

## Genuine gaps vs the goal

1. **Create-your-own connections (the foundation).** Missing on both lines. MCP wiring today is hand-edit a JSON file + set env + restart. There is no `iris mcp` command, no in-chat connect, no test-a-connection flow. This is the backbone of "all the connections like Claude Code" and of the no-hard-coding principle.
2. **De-hard-code publishing.** Both lines ship a hard-wired publisher (`social.py` on main, `buffer.py` on the fork). Publishing should become a user-added connection.
3. **The "outgoing, proactive worker" layer.** The autonomy loop exists (ticks, goals), but the initiative/personality that makes it feel like a proactive colleague rather than a cron is thin. `examples/standing-orders.example.md` is a seed to build on.
4. **Consolidation/positioning.** The capabilities exist but aren't presented as one coherent "comprehensive agent," and the two lines must become one.

## Decomposition (ordered sub-projects)

Each gets its own spec → plan → implementation cycle.

- **S0 — Reconcile onto `origin/main`** (prerequisite, mostly a decision + cleanup). Canonicalize main; close PR #13; retire the stale `buffer-publishing` branch; keep `budget.py`/Buffer *ideas* for later sub-projects. Small, but it unblocks everything and stops the divergence from growing.
- **S1 — Create Your Own Connections** (the foundation; full spec in `2026-06-21-create-your-own-connections-design.md`). First-class `iris mcp add/list/remove/enable/test/import`, connections as the first-class unit, allowlist kept in sync, internal servers reframed as optional examples, zero pre-set integrations.
- **S2 — Publishing as a connection.** Remove native `social.py`/`buffer.py` from core; publishing becomes a user-added MCP connection (e.g. a Buffer MCP, or any). Realizes the Buffer work the right way.
- **S3 — Outgoing proactive worker.** A personality/initiative layer over the existing proactive/goal loop: standing orders, proactive outreach, follow-through, voice. Make it feel like a colleague.
- **S4 — Connection ecosystem breadth.** Document and smooth the "all the connections like Claude Code" set (filesystem, browser, web search, GitHub, etc.) as user-added connections enabled by S1.
- **S5 — Polish/close-out.** Whatever `iris audit` and a fresh review surface after consolidation.

## Principle (applies to every sub-project)

Iris core ships **zero auto-loaded integrations**. The built-in MCP servers are optional connections the owner chooses to enable. Connection config is written only by the owner via the CLI (the model never writes it), consistent with the workspaces security model. The allowlist remains the execution boundary; `--strict-mcp-config` stays.

## Immediate next step

Spec and build **S1 (Create Your Own Connections)** on `origin/main`, after the owner confirms S0 (canonicalize main, retire the stale fork, close PR #13).
