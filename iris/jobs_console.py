"""The terminal job console: see and steer background jobs without the bot.

`iris jobs ...` reads the job registry directly (no model, no Discord round
trip, no credit) and lets the owner list, inspect, launch, cancel, resume,
re-run, and re-deliver jobs from the box where Iris runs. It is the
owner-at-the-keyboard equivalent of the jobs MCP tools.
See docs/superpowers/specs/2026-06-10-job-console-design.md.

The launch paths (`run`, `rerun`, `resume`) spawn the same detached runner as
the MCP `start_job`; the runner makes the one model call, the console makes
none. The only outbound network is the artifact re-delivery (`deliver`), the
existing Discord REST upload.
"""

from __future__ import annotations

from typing import Optional

from .config import Config
from .jobs import (
    JobStore,
    cancel,
    clamp_grants,
    parse_grants,
    repair_dead_runners,
    rerun_job,
    send_discord_file,
    spawn_runner,
)
from .reminders import fmt_ts
from .workspaces import WorkspaceStore, collect_artifacts

_DISABLED = "Background jobs are disabled (set IRIS_JOBS=true)."


def _age(job: dict) -> str:
    ts = job.get("finished_ts") or job.get("started_ts") or job.get("created_ts")
    return fmt_ts(ts) if ts else "?"


def format_table(jobs: list[dict]) -> str:
    """The `iris jobs` list view: one row per job, newest first."""
    if not jobs:
        return "No jobs recorded."
    rows = ["  ID  STATE      TITLE                          WHEN              GRANTS / WS"]
    for job in sorted(jobs, key=lambda j: j.get("id", 0), reverse=True):
        grants = ",".join(job.get("grants") or []) or "-"
        ws = job.get("workspace") or "-"
        rows.append(
            f"  #{job.get('id'):<3} {job.get('state',''):<10} "
            f"{(job.get('title') or '')[:30]:<30} {_age(job):<17} {grants} / {ws}"
        )
    return "\n".join(rows)


def format_detail(job: dict) -> str:
    """The `iris jobs show <id>` view."""
    lines = [f"Job #{job['id']} ({job.get('title','')}): {job.get('state','')}"]
    if job.get("workspace"):
        lines.append(f"workspace: {job['workspace']}")
    lines.append(f"grants: {', '.join(job.get('grants') or []) or '-'}")
    if job.get("started_ts"):
        lines.append(f"started: {fmt_ts(job['started_ts'])}")
    if job.get("finished_ts"):
        lines.append(f"finished: {fmt_ts(job['finished_ts'])}")
    if job.get("error"):
        lines.append(f"error: {job['error']}")
    if job.get("artifacts"):
        lines.append("artifacts: " + ", ".join(job["artifacts"]))
    lines.append("")
    lines.append("instructions:")
    lines.append(f"  {job.get('instructions','')}")
    report = (job.get("report") or "").strip()
    if report:
        lines.append("")
        lines.append("report:")
        lines.append(report)
    return "\n".join(lines)


def jobs_command(config: Config, args, *, spawn=None, send_file=None) -> int:
    """Dispatch an `iris jobs <action>` invocation. Returns the exit code.

    ``spawn`` and ``send_file`` are injectable seams (default to the real
    detached-runner spawn and the Discord file upload).
    """
    spawn = spawn or spawn_runner
    send_file = send_file or send_discord_file
    action = getattr(args, "jobs_action", None)

    store = JobStore(config.jobs_file, keep=config.jobs_keep)

    if action in (None, ""):
        print(
            "usage: iris jobs {list | show <id> | run --title T --instructions I "
            "[--grant g] [--workspace w] | cancel <id> | resume <id> | rerun <id> "
            "| artifacts <id> | deliver <id> | prune [--keep N] | --tui}"
        )
        return 2

    if action in ("list", "show", "artifacts", "cancel", "resume", "rerun", "deliver"):
        repair_dead_runners(store)

    if action == "list":
        print(format_table(store.all()))
        return 0

    if action == "prune":
        keep = config.jobs_keep if getattr(args, "keep", None) is None else int(args.keep)
        dropped = store.prune(keep)
        print(f"Pruned {dropped} terminal job(s); keeping the most recent {keep}.")
        return 0

    if action == "run":
        return _cmd_run(config, store, args, spawn)

    # The remaining actions need a job id.
    job_id = getattr(args, "job_id", None)
    if job_id is None:
        print(f"usage: iris jobs {action} <id>")
        return 2
    job = store.get(job_id)
    if job is None and action != "cancel":
        print(f"No job #{job_id}.")
        return 1

    if action == "show":
        print(format_detail(job))
        return 0
    if action == "artifacts":
        names = job.get("artifacts") or []
        print("\n".join(names) if names else "No artifacts for this job.")
        return 0
    if action == "cancel":
        print(cancel(store, job_id))
        return 0
    if action == "resume":
        return _cmd_resume(config, store, job, spawn)
    if action == "rerun":
        clone = rerun_job(store, job_id, config.home_channel)
        spawn(clone["id"], store=store)
        print(f"Re-ran job #{job_id} as new job #{clone['id']} ({clone['title']}).")
        return 0
    if action == "deliver":
        return _cmd_deliver(config, store, job, send_file)

    print(f"Unknown jobs action: {action}")
    return 2


def _cmd_run(config: Config, store: JobStore, args, spawn) -> int:
    if not config.jobs_enabled:
        print(_DISABLED)
        return 1
    title = (getattr(args, "title", None) or "").strip()
    instructions = getattr(args, "instructions", None) or ""
    if not title or not instructions.strip():
        print("A job needs both --title and --instructions.")
        return 2
    try:
        requested = parse_grants(getattr(args, "grant", "") or "")
    except ValueError as exc:
        print(str(exc))
        return 2
    granted, clamped = clamp_grants(requested, config.job_grants)
    workspace = (getattr(args, "workspace", "") or "").strip()
    if workspace and WorkspaceStore(config.workspaces_file).resolve(workspace) is None:
        names = ", ".join(WorkspaceStore(config.workspaces_file).list()) or "none registered"
        print(f"No workspace named {workspace!r} (registered: {names}).")
        return 2

    from .usage import CreditGuard

    if CreditGuard.from_config(config).should_park():
        job = store.add(title, instructions, granted, workspace, config.home_channel, state="parked")
        print(f"Job #{job['id']} ({title}) PARKED: the credit budget is nearly spent. "
              f"Launch it anyway with: iris jobs resume {job['id']}.")
        return 0
    repair_dead_runners(store)
    job = store.add(title, instructions, granted, workspace, config.home_channel,
                    admit_below=config.jobs_max)
    if not job["admitted"]:
        print(f"Job #{job['id']} ({title}) recorded but queued: {config.jobs_max} jobs are "
              f"already active. Launch it with: iris jobs resume {job['id']}.")
    else:
        spawn(job["id"], store=store)
        print(f"Started job #{job['id']} ({title}) with grants: {', '.join(granted)}.")
    if clamped:
        print(f"Refused grants over the IRIS_JOB_GRANTS ceiling: {', '.join(clamped)}.")
    return 0


def _cmd_resume(config: Config, store: JobStore, job: dict, spawn) -> int:
    if not config.jobs_enabled:
        print(_DISABLED)
        return 1
    if job["state"] not in ("pending", "parked"):
        print(f"Job #{job['id']} is {job['state']}; only parked or queued jobs can be resumed.")
        return 1
    store.transition(job["id"], ("parked",), "pending")
    spawn(job["id"], store=store)
    print(f"Resumed job #{job['id']} ({job.get('title','')}).")
    return 0


def _cmd_deliver(config: Config, store: JobStore, job: dict, send_file) -> int:
    names = job.get("artifacts") or []
    if not names:
        print("No artifacts to deliver for this job.")
        return 0
    channel = job.get("channel_id") or config.home_channel
    if not channel or not config.discord_token:
        print("No home channel / token configured; cannot deliver.")
        return 1
    workspace_path = None
    if job.get("workspace"):
        workspace_path = WorkspaceStore(config.workspaces_file).resolve(job["workspace"])
    # Re-resolve the names through the same containment-checked collector so a
    # since-moved or escaping artifact is refused, not blindly uploaded.
    report = "\n".join(f"ARTIFACT: {n}" for n in names)
    files, problems = collect_artifacts(report, workspace_path)
    for problem in problems:
        print(problem)
    for path in files:
        res = send_file(channel, path, f"job #{job['id']} artifact", config.discord_token)
        if isinstance(res, dict) and res.get("error"):
            print(f"upload failed for {path}: {res['error']}")
        else:
            print(f"delivered {path}")
    return 0
