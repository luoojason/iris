---
title: Hermes feature port (audit, mcp-gating, digest, approvals)
date: 2026-06-18
status: approved (design); features built in order
---

# Hermes feature port

Four features carried over from the Hermes/OpenClaw comparison in
`obsidian-vault/Projects/Iris Roadmap.md`, each filtered through Iris's
invariants: single-user (solo box), official `claude` binary only, owner-bound
names, dependency-free core, no idle inference, grants clamped, fail closed.

Built in order, each fully (design -> TDD -> green -> commit) before the next:
1. `iris audit` — model-free security/compliance self-audit.
2. Per-context MCP gating — jobs can't see job-spawning/schedule tools.
3. Session digest (`!digest`) — owner-invoked recap of the day's conversations.
4. Discord approval buttons — just-in-time Approve/Deny via `--permission-prompt-tool`.

Features 2-4 capture the approved approach here; each gets its detailed design
section filled in immediately before it is built.

---

## Feature 1 — `iris audit` (detailed, approved)

A model-free, owner-run audit that scans Iris's live security/compliance posture
and reports findings ranked by severity. It is to the §0 invariants and the
S1-S5 hardening what `doctor` is to "is claude installed": a standing check that
the boundaries have not regressed. No model call, no network — stat/read/inspect
only, so it is fully deterministic and unit-testable.

### Architecture

New pure module `iris/audit.py`:

- `Finding` dataclass: `severity` (one of `critical|high|medium|low|info`),
  `code` (stable slug, e.g. `secrets-mode`), `title`, `detail`, `fix`.
- One function per check: `check_<name>(config) -> list[Finding]`. Each reads
  config, file modes, mcp.json, and env only. Pure and individually testable.
- `run_audit(config) -> list[Finding]` runs every check, concatenates results.
- `render_audit(findings) -> str` groups by severity (critical first), with a
  one-line summary header (counts per severity).
- `worst_severity(findings) -> str` for the exit code.

CLI in `cli.py`: `iris audit [--json]`.
- Default: print `render_audit`. `--json`: dump the findings list.
- Exit code: non-zero (2) if any `critical` or `high` finding, else 0 — so it is
  usable from cron/CI as a tripwire.

### Checks

| code | severity | what it verifies |
|------|----------|------------------|
| `secrets-mode` | high | `~/.claude/.credentials.json` and `.env` are mode 600 (not group/world readable) |
| `secrets-world-readable` | high | no `.env*` (incl. `.bak`/`.backup`) is world-readable |
| `chat-sandbox` | critical | the chat driver's effective denylist blocks Bash/Write/Edit/Task (chat can never shell) |
| `chat-grants` | critical | chat config carries no shell/files/browser; job grants clamp to `IRIS_JOB_GRANTS` |
| `single-user` | medium | `IRIS_ALLOWED_USER_IDS` set; flags the empty + `respond_without_mention` combo (S1). Prints a "solo-operator: acknowledged" info line so it is not noise on a single-user box |
| `publish-dir` | high | if the publish tool is allow-listed, `IRIS_PUBLISH_DIR` is set (S5) |
| `usage-budget` | medium | `IRIS_USAGE_BUDGET_USD` > 0 so the credit-guard park backstop is armed when self-start is on |
| `trace-privacy` | medium | warns if `IRIS_TRACE_CAPTURE_CONTENT=true` (prompts/replies stored on disk) |
| `discord-reach` | info | server-actions tools are default-deny (S2) |
| `child-env` | info | `ANTHROPIC_*` / `IRIS_*` are stripped from the claude child env (`_SECRET_ENV_DROP`) |

Checks fail soft: an unreadable file or missing path yields an `info`/`low`
"could not check" finding, never an exception.

### Testing

Per-check unit tests with a `Config` + tmp files: good vs bad file modes, missing
`IRIS_PUBLISH_DIR` with publish allow-listed, a chat config that leaks a dangerous
tool, empty allowlist, budget unset, content-capture on. Assert the exact
`code` + `severity`. Plus: `render_audit` formatting, `worst_severity`, and the
CLI exit-code (0 clean / 2 when a high+ exists). ~15 focused tests. No model,
no network, no real `claude`.

---

## Feature 2 — Per-context MCP gating (approach approved; detail TBD before build)

Generate the job's tool surface so a job literally cannot see job-spawning or
schedule-creating tools (`start_job`, `run_in_background`, `schedule_job`,
`list_schedules`, `cancel_schedule`), enforcing "a job cannot schedule a job or
widen its grants" structurally, not just by clamping. Today only the
schedule-creation exclusion ships. Likely shape: a per-context allowed/denied
tool set (chat vs job) derived in one place, applied via the driver's
allowed/disallowed tool lists and/or a filtered mcp config. TDD against the
generated tool lists.

## Feature 3 — Session digest (`!digest`) (approach approved; detail TBD)

Owner-invoked recap of the day's conversations. A `!digest` bang command (and/or
`iris digest`) launches a single job that reads the day's session transcripts
via the existing `session_search` MCP and posts a recap to the home channel.
Owner-triggered (not the clock), so it stays inside the no-idle-inference
invariant. Reuses the job runner + fold-back delivery.

## Feature 4 — Discord approval buttons (approach approved; detail TBD)

Just-in-time Approve/Deny for risky tool calls via Claude Code's native
`--permission-prompt-tool` pointed at an approvals MCP server. The server posts
an Approve/Deny message to the owner's channel and blocks until the owner clicks;
owner-id-verified; fails closed on timeout. The hard part is cross-process
coordination (the MCP server posts via REST; the button click arrives at the bot;
they rendezvous through a small shared file/store). Most complex — designed in
full before build, with careful tests around the fail-closed and owner-verify
paths.
