"""MCP server: let the agent start, inspect, cancel, and resume background jobs.

The tools speak in job ids, grant names, and workspace names — never
filesystem paths, never raw channel ids. Everything is gated on IRIS_JOBS.
The server only records jobs and spawns the detached runner
(``python -m iris job-run``); the runner makes the one model call.
See docs/superpowers/specs/2026-06-08-job-coordinator-design.md.
"""

from __future__ import annotations

from typing import Optional

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Needs the MCP SDK: pip install 'iris-agent[memory]'") from exc

from iris.config import Config
from iris.jobs import (
    JobStore,
    clamp_grants,
    launch_ready_dependents,
    parse_grants,
    repair_dead_runners,
    spawn_runner,
)
from iris.reminders import fmt_ts
from iris.workspaces import WorkspaceStore

mcp = FastMCP("iris-jobs")

# Lazy config: this server is spawned by the claude child (IRIS_* stripped
# from its env), so the knobs come from .env in the working directory. Loading
# lazily keeps module import free of environment side effects for tests.
_CONFIG: Optional[Config] = None

SPAWN = spawn_runner  # test seam


def _config() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config.from_env()
    return _CONFIG


def _store() -> JobStore:
    cfg = _config()
    return JobStore(cfg.jobs_file, keep=cfg.jobs_keep)


def _workspaces() -> WorkspaceStore:
    return WorkspaceStore(_config().workspaces_file)


def _kill_runner(pid) -> bool:
    # A thin module-level seam (tests monkeypatch this) over the shared killer.
    from iris.jobs import kill_process_group

    return kill_process_group(pid)


@mcp.tool()
def start_job(title: str, instructions: str, grants: str = "", workspace: str = "",
              heavy: bool = False, after: int = 0, force: bool = False) -> str:
    """Start a background job: one deep claude run, detached from this chat.

    Use it for work that takes minutes (audits, refactors, research). The job
    can spawn its own subagents. You will get a ping when it finishes and the
    report folds into the owner's next message.

    Args:
        title: A short label for the job.
        instructions: The full prompt for the job to execute.
        grants: Extra capabilities, comma-separated: 'shell', 'files'.
            Subagents are always granted. The owner's IRIS_JOB_GRANTS ceiling
            caps what is actually given.
        workspace: A registered workspace name the job may work in. Ask the
            owner to register one (iris workspaces add) if none fits.
        heavy: Set True ONLY for genuinely hard jobs (deep reasoning, tricky
            multi-step work) to run them on the stronger model. Everyday jobs
            leave this False and run on the cheaper default model.
        after: Chain this job to run only after job #<after> finishes
            successfully. It waits until then (and is cancelled if that job
            fails). Use it to sequence dependent work.
        force: Launch even if a near-identical job is already active or just
            finished. Leave False; only set it when you truly mean to run the
            same work again.
    """
    config = _config()
    if not config.jobs_enabled:
        return _DISABLED
    if not (title or "").strip() or not (instructions or "").strip():
        return "A job needs both a title and instructions."
    try:
        requested = parse_grants(grants)
    except ValueError as exc:
        return str(exc)
    granted, clamped = clamp_grants(requested, config.job_grants)
    import os
    # Report back to the thread this job was started from (set by the driver),
    # falling back to the home channel for non-Discord or unknown origins.
    origin = os.environ.get("IRIS_ORIGIN_CHANNEL") or config.home_channel
    store = _store()
    # Soft de-dup: a near-identical job already running (or just finished) on this
    # channel is almost always an accidental re-launch (see the #27/#28 double
    # upload). Advisory, force-overridable — never a hard block on a real re-run.
    if not force:
        from iris.jobs import find_duplicate_job
        dup = find_duplicate_job(store, title, origin)
        if dup is not None:
            return (f"A near-identical job #{dup['id']} ({dup.get('title')}) is already "
                    f"{dup['state']}. Not launching a duplicate. Check it with job_status"
                    f"({dup['id']}), or pass force=true if you really mean to run it again.")
    if workspace:
        if _workspaces().resolve(workspace) is None:
            names = ", ".join(_workspaces().list()) or "none registered"
            return (
                f"No workspace named {workspace!r} (registered: {names}). "
                "The owner registers one with: iris workspaces add <name> <path>."
            )

    if after:
        if store.get(after) is None:
            return f"No job #{after} to chain after."
        job = store.add(title.strip(), instructions, granted, workspace, origin,
                        state="waiting", heavy=heavy, after=after)
        # Resolve immediately: if the prerequisite already finished, this launches
        # the dependent now; otherwise it stays waiting until that job completes.
        launch_ready_dependents(store, config, spawn=SPAWN)
        state_now = (store.get(job["id"]) or {}).get("state")
        lines = []
        if state_now == "waiting":
            lines.append(f"Job #{job['id']} ({job['title']}) queued to run after "
                         f"job #{after} finishes.")
        elif state_now == "pending":
            lines.append(f"Job #{job['id']} ({job['title']}) started: prerequisite "
                         f"job #{after} was already done.")
        else:
            lines.append(f"Job #{job['id']} ({job['title']}) is {state_now}.")
        if clamped:
            lines.append(f"Refused grants (over IRIS_JOB_GRANTS): {', '.join(clamped)}.")
        return "\n".join(lines)

    from iris.usage import CreditGuard

    lines = []
    if CreditGuard.from_config(config).should_park():
        job = store.add(title.strip(), instructions, granted, workspace,
                        origin, state="parked", heavy=heavy)
        lines.append(
            f"Job #{job['id']} ({job['title']}) was PARKED, not started: the credit "
            f"guard says the month's budget is nearly spent. The owner can launch "
            f"it anyway with resume_job({job['id']})."
        )
    else:
        repair_dead_runners(store)
        launch_ready_dependents(store, config, spawn=SPAWN)  # progress any ready chains
        # The admission check runs inside the store's lock with the insert,
        # so two simultaneous start_job calls cannot both slip under the cap.
        job = store.add(title.strip(), instructions, granted, workspace,
                        origin, admit_below=config.jobs_max, heavy=heavy)
        if not job["admitted"]:
            lines.append(
                f"Job #{job['id']} ({job['title']}) recorded but queued: "
                f"{config.jobs_max} jobs are already active. "
                f"Start it later with resume_job({job['id']})."
            )
        else:
            SPAWN(job["id"], store=store)
            lines.append(
                f"Job #{job['id']} ({job['title']}) started in the background "
                f"with grants: {', '.join(granted)}."
            )
    if clamped:
        lines.append(
            f"Refused grants (over the owner's IRIS_JOB_GRANTS ceiling): {', '.join(clamped)}."
        )
    return "\n".join(lines)


_DISABLED = "Background jobs are disabled. The owner can set IRIS_JOBS=true."


@mcp.tool()
def job_status(job_id: int) -> str:
    """Check one background job: state, timing, report, artifacts."""
    if not _config().jobs_enabled:
        return _DISABLED
    store = _store()
    repair_dead_runners(store)
    job = store.get(job_id)
    if job is None:
        return f"No job #{job_id}."
    lines = [f"Job #{job['id']} ({job['title']}): {job['state']}"]
    if job.get("started_ts"):
        lines.append(f"started {fmt_ts(job['started_ts'])}")
    if job.get("finished_ts"):
        lines.append(f"finished {fmt_ts(job['finished_ts'])}")
    if job.get("error"):
        lines.append(f"error: {job['error']}")
    if job.get("artifacts"):
        lines.append("artifacts: " + ", ".join(job["artifacts"]))
    if job.get("verified") is False:
        reason = (job.get("verify_reason") or "").strip()
        lines.append("verification: an independent check flagged this result as "
                     "possibly not satisfying the task." + (f" ({reason})" if reason else ""))
    elif job.get("verified") is True:
        lines.append("verification: independently checked, looks good.")
    report = (job.get("report") or "").strip()
    if report:
        lines.append("report:")
        lines.append(report[:1500] + (" …[truncated]" if len(report) > 1500 else ""))
    return "\n".join(lines)


@mcp.tool()
def list_jobs(limit: int = 10) -> str:
    """List recent background jobs, newest first."""
    if not _config().jobs_enabled:
        return _DISABLED
    store = _store()
    repair_dead_runners(store)
    jobs = store.all()
    if not jobs:
        return "No jobs recorded."
    lines = []
    for job in reversed(jobs[-max(1, int(limit)):]):
        when = job.get("finished_ts") or job.get("started_ts") or job.get("created_ts")
        lines.append(f"#{job['id']} [{job['state']}] {job['title']} ({fmt_ts(when)})")
    return "\n".join(lines)


@mcp.tool()
def cancel_job(job_id: int) -> str:
    """Cancel a background job (kills its runner and its claude turn)."""
    if not _config().jobs_enabled:
        return _DISABLED
    from iris.jobs import cancel as cancel_core

    return cancel_core(_store(), job_id, kill=_kill_runner)


@mcp.tool()
def resume_job(job_id: int, answer: str = "") -> str:
    """Launch a parked/queued job, or answer a job that paused to ask you.

    For a job waiting on your input (state needs_input), pass ``answer`` with your
    decision; the job resumes the same session with full context. For a parked or
    queued job, call it with no answer to launch it now.
    """
    config = _config()
    if not config.jobs_enabled:
        return _DISABLED
    from iris.jobs import resume_job as resume_core
    reply = resume_core(_store(), job_id, answer=answer, spawn=SPAWN)
    # The chat tool nudges the owner toward the answer= form for a paused job.
    if reply.startswith(f"Job #{job_id} is waiting for your answer"):
        reply += f"\nCall resume_job({job_id}, answer=...) with your decision."
    return reply


_SCHEDULES_DISABLED = ("Scheduled jobs are disabled. The owner can set "
                       "IRIS_SCHEDULED_JOBS=true (jobs too: IRIS_JOBS=true).")

# Most schedule rules the model may have recorded at once. The per-rule
# monthly cap bounds nothing in aggregate, so the recorder itself is capped:
# a runaway or prompt-injected turn cannot mint unbounded clock-driven work.
# None = read IRIS_SCHEDULES_MAX_MODEL_RULES lazily (default 10).
MAX_MODEL_RULES: Optional[int] = None


def _max_model_rules() -> int:
    if MAX_MODEL_RULES is not None:
        return MAX_MODEL_RULES
    import os

    return int(os.environ.get("IRIS_SCHEDULES_MAX_MODEL_RULES", "10"))


def _schedule_store():
    from iris.schedules import ScheduleStore

    return ScheduleStore(_config().schedules_file)


@mcp.tool()
def schedule_job(title: str, instructions: str, when: str, every: str = "",
                 grants: str = "", workspace: str = "") -> str:
    """Record a scheduled job: the clock will launch it as a background job.

    Only schedule what the owner explicitly asked to have run on a clock; the
    instructions are recorded verbatim and fire without further review. Each
    firing is a normal background job (grants clamped, credit-guard parked,
    capped per month).

    Args:
        title: A short label for the schedule.
        when: First firing: +30m, +2h, +1d, or an ISO datetime (UTC).
        every: Recurrence: 'every 30m', 'every 2h', 'every 1d'. Omit for one-shot.
        instructions: The full prompt the scheduled job will run.
        grants: Extra capabilities, comma-separated: 'shell', 'files'.
        workspace: A registered workspace name the job may work in.
    """
    config = _config()
    if not (config.jobs_enabled and config.scheduled_jobs_enabled):
        return _SCHEDULES_DISABLED
    from iris.schedules import add_rule, describe_rule

    store = _schedule_store()
    mine = sum(1 for r in store.all()
               if isinstance(r, dict) and r.get("created_by") == "model")
    if mine >= _max_model_rules():
        return (f"You already have {mine} recorded schedules, the most allowed. "
                "Cancel one with cancel_schedule before recording more.")
    try:
        rule = add_rule(
            store, title=title, when=when, every=every,
            instructions=instructions, grants=grants, workspace=workspace,
            created_by="model", default_cap=config.schedule_monthly_cap,
        )
    except ValueError as exc:
        return str(exc)
    return f"Recorded schedule {describe_rule(rule)}"


@mcp.tool()
def list_schedules() -> str:
    """List the recorded schedule rules."""
    config = _config()
    if not (config.jobs_enabled and config.scheduled_jobs_enabled):
        return _SCHEDULES_DISABLED
    from iris.schedules import describe_rule

    rules = _schedule_store().all()
    if not rules:
        return "No schedules recorded."
    return "\n".join(describe_rule(rule) for rule in rules)


@mcp.tool()
def cancel_schedule(rule_id: int) -> str:
    """Remove a schedule rule by id."""
    config = _config()
    if not (config.jobs_enabled and config.scheduled_jobs_enabled):
        return _SCHEDULES_DISABLED
    if _schedule_store().remove(rule_id):
        return f"Cancelled schedule #{rule_id}."
    return f"No schedule #{rule_id}."


def _launch_watch(argv, cwd):
    """Spawn a detached ``iris watch`` (test seam). start_new_session detaches
    it into its own process group so it survives this turn ending."""
    import subprocess

    subprocess.Popen(
        argv, cwd=cwd,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


@mcp.tool()
def run_in_background(command: str, label: str = "", workspace: str = "",
                     autoresume: bool = False) -> str:
    """Run a long shell command detached and get pinged when it finishes.

    Use this for long COMPUTE that should not occupy a chat turn or a job (both
    time out): video builds and renders, big downloads, batch processing. The
    command runs to completion however long it takes - no model turn is held
    open, so there is no timeout - and the home channel is pinged on success or
    failure. This is for shell work, not browser work.

    Args:
        command: the shell command to run.
        label: a short name for the completion ping.
        workspace: a registered workspace name to run it in (its directory
            becomes the working directory; e.g. "clipper" for video builds).
        autoresume: set True when this is a step in a chain you should continue
            on your own - when it finishes, you'll get one follow-up turn in the
            home channel with the result, so you can do the next step (e.g.
            schedule the uploads after a build) without the owner poking you.
            Default False just pings. Inert unless the owner enabled it.
    """
    import os
    import shlex
    import sys

    config = _config()
    if not config.jobs_enabled:
        return _DISABLED
    if "shell" not in config.job_grants:
        return ("Running background commands needs the 'shell' grant in the "
                "owner's IRIS_JOB_GRANTS ceiling.")
    if not (command or "").strip():
        return "Give a command to run."
    inner = command
    if workspace:
        path = _workspaces().resolve(workspace)
        if path is None:
            names = ", ".join(_workspaces().list()) or "none registered"
            return f"No workspace named {workspace!r} (registered: {names})."
        inner = f"cd {shlex.quote(path)} && {command}"
    name = ((label or command).strip())[:60]
    # The conversation this tool call belongs to (a thread), so the completion
    # ping and any resume turn report BACK HERE instead of the home channel. The
    # driver re-adds IRIS_ORIGIN_CHANNEL after stripping IRIS_* from the server
    # env; it is empty for a non-Discord/clock origin, which falls back to home.
    origin = os.environ.get("IRIS_ORIGIN_CHANNEL", "").strip()
    # iris watch runs the command and pings on completion (templated on success,
    # one model call to interpret a failure). It loads .env from this server's
    # cwd, so spawn with the inherited cwd.
    argv = [sys.executable, "-m", "iris", "watch", "--name", name, "--fold"]
    # Only arm autonomous resume when the owner turned it on AND there is a
    # conversation (this thread, else the home channel) for the resume to land
    # in — the same conditions watch checks before it enqueues. Keeping the argv
    # and the reply honest means the model is never told the chain self-continues
    # when it actually won't.
    will_resume = bool(autoresume and config.auto_resume and (origin or config.home_channel))
    if will_resume:
        argv.append("--resume")
    if origin:
        argv += ["--channel", origin]
    argv += ["--", "/bin/sh", "-c", inner]
    _launch_watch(argv, None)
    where = "here" if origin else "the home channel"
    base = (f"Started '{name}' in the background. It runs to completion however long "
            f"it takes (no model turn is held open, so no timeout), and I will ping "
            f"{where} when it finishes.")
    if will_resume:
        return base + " When it finishes I'll continue the chain automatically and report back."
    if autoresume:
        return base + " (Auto-resume is off, so I'll ping with the result instead of continuing on my own.)"
    return base


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
