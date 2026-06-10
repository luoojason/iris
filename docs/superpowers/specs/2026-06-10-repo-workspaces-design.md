# Repo workspaces and artifact delivery (core) — design

Date: 2026-06-10
Status: building (backlog item 4, repo-side core; console surfaces come later)

## Context

Jobs currently run in the bot's cwd, so "fix the flaky test in geosql" cannot
actually touch geosql, and a job that produces a CSV or screenshot strands it
on the box. This sub-project adds owner-bound workspaces (jobs may run inside
a registered checkout) and a deterministic artifact hand-back path. The
Discord `/bind` command and Artifacts buttons belong to the console build;
this is the engine underneath.

## Security model (the part that matters)

The model NEVER names a path. A job may only request a workspace by NAME, and
names are bound to paths exclusively by the owner via the CLI. Resolution
happens bot-side at spawn; unknown names fail the job at start with a clear
error. Artifact uploads are restricted to files inside the job's resolved
workspace (or the attachments dir), size- and count-capped.

## Components

1. **Driver cwd** (`iris/driver.py`, `iris/stream_driver.py`): new
   `ClaudeDriver.cwd: Optional[str] = None` dataclass field; `_subprocess_run`
   passes `cwd=self.cwd` to Popen; `_default_spawn` and `StreamDriver.start`
   thread it through as a keyword with default None (the two existing test
   fakes gain the kwarg). No cwd set = exactly today's behavior.
2. **WorkspaceStore** (`iris/workspaces.py`): JSON registry (atomic writes,
   same shape discipline as SessionStore): name -> {path, added_at}. API:
   add(name, path) (validates the path exists and is a directory, resolves to
   absolute, name is [a-z0-9-]{1,32}), remove(name), get(name), all().
   Default path `iris-workspaces.json` (IRIS_WORKSPACES_FILE).
3. **CLI** (`iris/cli.py`): `iris workspaces add NAME PATH | remove NAME |
   list`. Owner-only by construction (local shell).
4. **Job plumbing**: job records gain `workspace: str = ""` (JobStore.add
   kwarg). `mcp__jobs__spawn_job` gains `workspace: str = ""` (docstring:
   request by name; the owner binds names with `iris workspaces add`).
   `iris jobs spawn --workspace NAME`. JobRunner resolves the name via
   WorkspaceStore at spawn: found -> `build_job_driver` gets `cwd=path`
   (dataclasses.replace carries it); missing -> job fails at start with
   "unknown workspace 'X'; bind it with: iris workspaces add" (spine ping as
   usual). `iris doctor` warns when IRIS_JOBS is on and a registry job names
   a workspace that is no longer bound.
5. **Artifacts**: convention, not magic. The JOB_PREAMBLE grows one line:
   files the owner should receive are listed at the end of the report, one
   per line, as `ARTIFACT: <absolute path>`. After delivery, JobRunner parses
   `ARTIFACT:` lines from result.text (max 5), keeps only files that exist,
   are <= 8MB, and resolve inside the job's workspace (or the attachments
   dir; jobs with no workspace get no uploads), and uploads each via a new
   `send_discord_file(channel_id, path, token)` in `iris/reminders.py` style:
   urllib multipart/form-data POST, 20s timeout, False on any failure,
   injectable for tests. Uploads target the job's channel_id else
   notify_channel; failures log a warning and never re-run the job. Skipped
   artifacts (missing/too big/outside the boundary) are reported in one
   templated line so the owner knows what did not arrive.
6. **Config**: IRIS_WORKSPACES_FILE; artifact caps as module constants
   (MAX_ARTIFACTS=5, MAX_ARTIFACT_BYTES=8MB) until proven to need knobs.

## Zero-idle and compliance

No new model calls anywhere: workspace resolution, artifact parsing, and
uploads are deterministic post-delivery file work. Single-user: binding is
local-CLI-only; the model can only point at names the owner created. The
upload helper is bot-side REST (the MCP sandbox never sees the token).

## Testing

Driver cwd argv/Popen tests (fake runner records cwd; no cwd = None);
WorkspaceStore validation/persistence; CLI add/remove/list with exit codes;
spawn-with-workspace resolution into the captured job driver (existing
FakeStreamDriver seams); unknown-workspace failure path; ARTIFACT parsing
(caps, traversal attempts via .. and symlinks rejected, nonexistent skipped);
upload helper with an injected poster (multipart body shape, failure ->
False); end-to-end runner test: done job with two artifacts uploads both and
reports one skipped.

## Out of scope

Discord /bind and per-thread default workspaces (console); git operations
(clone/pull stay manual or future); Telegram artifact parity; per-workspace
tool policies.
