"""The bang-command control plane: steer Iris from chat without a model turn.

A message like ``!usage`` or ``!stop 7`` is intercepted before the brain ever
runs, so the control plane costs zero inference. Every command here either
reads a state file (usage, jobs, schedules) or acts on a process/session
(stop a reply, cancel a job, reset the conversation) — never the model.

This module is pure and SDK-free so it is unit-testable: the adapter parses a
message, calls :func:`dispatch`, and sends back the string. The two
side-effecting commands that touch live adapter state (``!new`` resets the
session, bare ``!stop`` cancels the in-flight reply) are injected as callables,
so even dispatch is testable with fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .config import Config

# Aliases fold onto canonical command names. reset/forget/newchat predate this
# module (the adapter already honored them); cancel is the natural word for the
# job kill switch, which is just `stop <id>`.
_ALIASES = {
    "reset": "new",
    "forget": "new",
    "newchat": "new",
    "cancel": "stop",
}

# Commands that take no argument; a trailing word means it is prose, not a
# command, so it falls through to the brain. Only `stop` takes an optional arg
# (a job id), so it is excluded from this set.
_NO_ARG = frozenset({"help", "new", "status", "usage", "jobs", "schedules"})
_KNOWN = _NO_ARG | {"stop"}

HELP = (
    "Commands (instant, no AI turn):\n"
    "!usage - this month's spend and pace\n"
    "!jobs - recent background jobs\n"
    "!stop - stop the reply I'm writing here\n"
    "!stop <id> - cancel background job #id (alias: !cancel <id>)\n"
    "!schedules - scheduled jobs\n"
    "!status - what I'm doing right now\n"
    "!new - start a fresh conversation here\n"
    "!help - this list"
)


@dataclass
class Command:
    name: str
    arg: str


def parse(text: str) -> Optional[Command]:
    """Parse a message into a Command, or None if it is not a bang command.

    Conservative on purpose: an unknown ``!word`` and a no-arg command with
    trailing prose (``!help me debug this``) both return None so a real message
    is never swallowed by the control plane.
    """
    s = (text or "").strip()
    if len(s) < 2 or not s.startswith("!"):
        return None
    parts = s[1:].split(None, 1)
    name = _ALIASES.get(parts[0].lower(), parts[0].lower())
    if name not in _KNOWN:
        return None
    arg = parts[1].strip() if len(parts) > 1 else ""
    if arg and name in _NO_ARG:
        return None  # trailing text on a no-arg command -> it is prose
    return Command(name, arg)


def render_usage(config: Config) -> str:
    from .usage import summary_text

    return summary_text(config)


def render_jobs(config: Config, limit: int = 10) -> str:
    if not config.jobs_enabled:
        return "Background jobs are off (set IRIS_JOBS)."
    from .jobs import JobStore, repair_dead_runners
    from .reminders import fmt_ts

    store = JobStore(config.jobs_file)
    repair_dead_runners(store)
    jobs = store.all()
    if not jobs:
        return "No jobs recorded."
    lines = []
    for job in reversed(jobs[-max(1, int(limit)):]):
        when = job.get("finished_ts") or job.get("started_ts") or job.get("created_ts")
        lines.append(f"#{job['id']} [{job['state']}] {job['title']} ({fmt_ts(when)})")
    return "\n".join(lines)


def render_schedules(config: Config) -> str:
    if not (config.jobs_enabled and config.scheduled_jobs_enabled):
        return "Scheduled jobs are off (set IRIS_SCHEDULED_JOBS)."
    from .schedules import ScheduleStore, describe_rule

    rules = ScheduleStore(config.schedules_file).all()
    if not rules:
        return "No schedules recorded."
    return "\n".join(describe_rule(r) for r in rules)


def render_status(config: Config, *, busy: bool, pending: int, session_turns: int) -> str:
    parts = ["writing a reply now" if busy else "idle here"]
    if pending:
        parts.append(f"{pending} message(s) queued")
    if session_turns:
        parts.append(f"{session_turns} turns in this conversation")
    if config.jobs_enabled:
        from .jobs import JobStore

        active = JobStore(config.jobs_file).count_active()
        parts.append(f"{active} background job(s) active")
    return "; ".join(parts)


def cancel_job(config: Config, arg: str) -> str:
    if not config.jobs_enabled:
        return "Background jobs are off (set IRIS_JOBS)."
    try:
        job_id = int(arg)
    except (TypeError, ValueError):
        return f"Not a job id: {arg!r}. Use !stop <number>, or !jobs to see them."
    from .jobs import JobStore, cancel

    return cancel(JobStore(config.jobs_file), job_id)


def dispatch(
    cmd: Command,
    config: Config,
    *,
    reset: Callable[[], None],
    stop: Callable[[], str],
    status_fields: Callable[[], dict],
) -> str:
    """Run a parsed command and return the reply text.

    ``reset`` and ``stop`` are adapter-provided side effects (they touch the
    live conversation), so this stays free of any chat SDK.
    """
    name = cmd.name
    if name == "help":
        return HELP
    if name == "usage":
        return render_usage(config)
    if name == "jobs":
        return render_jobs(config)
    if name == "schedules":
        return render_schedules(config)
    if name == "status":
        return render_status(config, **status_fields())
    if name == "new":
        reset()
        return "Started a fresh conversation."
    if name == "stop":
        if cmd.arg:
            return cancel_job(config, cmd.arg)
        return stop()
    return HELP  # unreachable: parse() only yields known names
