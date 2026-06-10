"""MCP server: let the agent queue, inspect, and cancel background jobs.

Registry writer ONLY: the bot's runner is the sole spawner, and this process
may be SIGKILLed mid-call at any time, so every state change goes through
JobStore's atomic writes and every tool returns a friendly string, never
raises. Test seams: ``STORE`` (monkeypatched to a tmp_path JobStore) and
``_now`` (the module's one clock).
"""

from __future__ import annotations

import os
import time

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover - depends on optional extra
    raise SystemExit(
        "The jobs tool needs the MCP SDK. Install it with:\n"
        "    pip install mcp\n"
        "or install Iris with the memory extra: pip install 'iris-agent[memory]'"
    ) from exc

from iris.driver import DANGEROUS_BUILTINS
from iris.jobs import MAX_TIMEOUT_MINUTES, JobStore

STORE = JobStore(os.environ.get("IRIS_JOBS_FILE", "iris-jobs.json"))

mcp = FastMCP("iris-jobs")

# A driver retry can re-run a tool call's side effects, so an identical
# pending title+prompt younger than this is treated as the same request.
DUPLICATE_WINDOW_S = 5.0
VALID_STATUSES = ("pending", "running", "done", "failed", "cancelled", "interrupted")


def _now() -> float:
    """The module's single clock; tests monkeypatch this."""
    return time.time()


def _fmt_age(seconds: float) -> str:
    """Render an age in the reminders style: whole hours past 60m, else minutes."""
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}h" if seconds >= 3600 else f"{seconds // 60}m"


@mcp.tool()
def spawn_job(prompt: str, title: str = "", model: str = "",
              timeout_minutes: int = 0, grants: str = "",
              workspace: str = "") -> str:
    """Queue a background job: a fresh, autonomous claude run tracked by the bot.

    Delegate any work expected to take more than a minute or so (research,
    refactors, long analyses) instead of doing it inline: you keep chatting
    with the owner while the job runs, and its result is delivered back to
    the conversation automatically when it finishes.

    Args:
        prompt: Full instructions for the worker. It starts a fresh session
            with none of this conversation's context, so spell everything out.
        title: Short label for job listings; defaults to the prompt's first line.
        model: Optional model override; empty uses the default job model.
        timeout_minutes: Time budget in minutes (max 240); 0 uses the default
            (30 minutes).
        grants: Comma-separated normally-denied tools to request, e.g. 'Task'
            (or its alias 'Agent') so the job can fan out into subagents.
            Subject to the operator's grant ceiling.
        workspace: Optional workspace NAME the job runs inside (its working
            directory, e.g. a repo checkout). Request by name only; the owner
            binds names to paths with 'iris workspaces add'. Unknown names
            fail the job at start.
    """
    if not (prompt or "").strip():
        return "Job needs a prompt; nothing queued."
    granted = [g.strip() for g in (grants or "").split(",") if g.strip()]
    for name in granted:
        if name not in DANGEROUS_BUILTINS:
            return (f"Unknown grant {name!r}; valid grants: "
                    f"{', '.join(DANGEROUS_BUILTINS)}.")
    minutes = int(timeout_minutes or 0)
    timeout_s = min(minutes, MAX_TIMEOUT_MINUTES) * 60 if minutes > 0 else None
    label = (title or "").strip() or prompt.strip().splitlines()[0][:60]
    now = _now()
    for job in STORE.all(status="pending"):
        if (job.get("title") == label and job.get("prompt") == prompt
                and now - float(job.get("created_at") or 0.0) < DUPLICATE_WINDOW_S):
            return f"Job #{job['id']} already queued: {label}"
    job_id = STORE.add(prompt, label, model=(model or "").strip(),
                       timeout_s=timeout_s, grants=granted,
                       workspace=(workspace or "").strip())
    if granted:
        # This server cannot see IRIS_JOB_GRANTS, so it must not pretend the
        # grant is effective; the runner applies the ceiling at spawn.
        return (f"Job #{job_id} queued: {label} (grants recorded; the runner "
                f"applies the operator ceiling)")
    return f"Job #{job_id} queued: {label}"


@mcp.tool()
def list_jobs(status: str = "") -> str:
    """List background jobs, newest first, with status and age.

    Args:
        status: Optional filter: pending, running, done, failed, cancelled,
            or interrupted. Empty lists everything.
    """
    wanted = (status or "").strip().lower()
    if wanted and wanted not in VALID_STATUSES:
        return f"Unknown status {wanted!r}; valid: {', '.join(VALID_STATUSES)}."
    jobs = STORE.all(status=wanted or None)
    if not jobs:
        return f"No {wanted} jobs." if wanted else "No jobs."
    now = _now()
    lines = []
    for job in reversed(jobs):  # all() sorts by id, so newest first
        stamp = job.get("started_at")
        if stamp is None:
            stamp = job.get("created_at")
        if stamp is None:
            stamp = now
        lines.append(f"#{job['id']} [{job.get('status')} {_fmt_age(now - stamp)}] "
                     f"{job.get('title') or '(untitled)'}")
    return "\n".join(lines)


@mcp.tool()
def job_status(job_id: int) -> str:
    """Show one job's full detail: status, timing, model, grants, and any error.

    Args:
        job_id: The job id from spawn_job or list_jobs.
    """
    job = STORE.get(int(job_id))
    if job is None:
        return f"No job #{job_id}."
    now = _now()
    lines = [f"Job #{job['id']}: {job.get('title') or '(untitled)'}",
             f"status: {job.get('status')}"]
    for field, stamp in (("created", job.get("created_at")),
                         ("started", job.get("started_at")),
                         ("finished", job.get("finished_at"))):
        if stamp is not None:
            lines.append(f"{field} {_fmt_age(now - float(stamp))} ago")
    if job.get("model"):
        lines.append(f"model: {job['model']}")
    if job.get("grants"):
        lines.append(f"grants: {', '.join(job['grants'])}")
    if job.get("cancel_requested"):
        lines.append("cancel requested")
    error = (job.get("result") or {}).get("error")
    if job.get("status") == "failed" and error:
        lines.append(f"error: {error}")
    return "\n".join(lines)


@mcp.tool()
def cancel_job(job_id: int) -> str:
    """Cancel a job: a pending job is dropped, a running one is asked to stop.

    Args:
        job_id: The job id to cancel.
    """
    return STORE.request_cancel(int(job_id))


@mcp.tool()
def job_result(job_id: int, max_chars: int = 4000) -> str:
    """Fetch a finished job's result text.

    Args:
        job_id: The job id from spawn_job or list_jobs.
        max_chars: Truncate the result past this many characters
            (clamped between 200 and 20000).
    """
    job = STORE.get(int(job_id))
    if job is None:
        return f"No job #{job_id}."
    status = job.get("status")
    if status in ("pending", "running"):
        return f"Job #{job_id} is {status} (no result yet)."
    result = job.get("result") or {}
    text = (result.get("text") or "").strip()
    if status == "failed":
        return f"Job #{job_id} failed: {result.get('error') or 'unknown error'}"
    if not text:
        return f"Job #{job_id} is {status}; no result was recorded."
    limit = max(200, min(20_000, int(max_chars)))
    if len(text) > limit:
        return text[:limit] + f"\n... (truncated; {len(text)} chars total)"
    return text


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
