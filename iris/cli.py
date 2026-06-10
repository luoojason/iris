"""Command line surface: run the bot, chat in the terminal, or check setup."""

from __future__ import annotations

import argparse
import itertools
import logging
import shutil
import subprocess
import sys
import threading
import time

from .agent import Agent
from .config import Config
from .driver import ClaudeError

try:  # enables arrow keys, line editing, and history in the plain REPL
    import readline  # noqa: F401
except ImportError:
    pass


class _Spinner:
    """A tiny one-line 'thinking' spinner, shown only on an interactive tty."""

    def __init__(self, label: str = "thinking"):
        self.label = label
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._active = sys.stdout.isatty()

    def __enter__(self) -> "_Spinner":
        if not self._active:
            return self

        def spin() -> None:
            for ch in itertools.cycle("|/-\\"):
                if self._stop.is_set():
                    break
                sys.stdout.write(f"\riris > {self.label} {ch}")
                sys.stdout.flush()
                time.sleep(0.12)

        self._thread = threading.Thread(target=spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=0.3)
        if self._active:
            sys.stdout.write("\r" + " " * 40 + "\r")
            sys.stdout.flush()


def _mcp_config_has_jobs_server(mcp_config: str | None) -> bool:
    """True when the mcp config is readable JSON whose mcpServers has 'jobs'."""
    if not mcp_config:
        return False
    import json

    try:
        with open(mcp_config, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return False
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return isinstance(servers, dict) and "jobs" in servers


def doctor(config: Config, probe: bool = True) -> int:
    """Verify the claude binary is present and actually signed in."""
    path = shutil.which(config.claude_bin)
    if not path:
        print(f"claude binary not found: {config.claude_bin!r}")
        print("Install Claude Code and sign in to your subscription first.")
        return 1
    print(f"claude found: {path}")
    try:
        version = subprocess.run([config.claude_bin, "--version"], capture_output=True, text=True, timeout=30)
        print(f"version: {version.stdout.strip() or version.stderr.strip()}")
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"could not run claude --version: {exc}")
        return 1
    # --version is a local check that passes even when logged out, so do one tiny
    # real turn to actually confirm the subscription is signed in and has credit.
    if probe:
        from .driver import ClaudeDriver, ClaudeError

        print("checking sign-in (one small metered call)...")
        prober = ClaudeDriver(
            claude_bin=config.claude_bin,
            model=config.model,
            timeout=60,
            max_retries=0,
            timeout_max_retries=0,
        )
        try:
            res = prober.run("Reply with just: ok")
        except ClaudeError as exc:
            print(f"  sign-in check FAILED: {exc}")
            return 1
        if res.is_error:
            print(f"  sign-in check FAILED: {res.error}")
            print("  Run 'claude' once to sign in, and claim your monthly agent credit.")
            return 1
        print(f"  signed in (model: {res.model or 'claude default'})")
    print(f"model: {config.model or '(claude default)'}")
    print(f"persona: {config.persona_file or '(none)'}")
    print(f"mcp tools: {config.mcp_config or '(none)'}")
    print(f"allowed tools: {', '.join(config.allowed_tools) if config.allowed_tools else '(none)'}")
    print(f"voice transcription: {'on (' + config.voice_model + ')' if config.voice_enabled else 'off'}")
    if config.compact_at_tokens or config.compact_every:
        triggers = []
        if config.compact_at_tokens:
            triggers.append(f"{config.compact_at_tokens} tokens")
        if config.compact_every:
            triggers.append(f"{config.compact_every} turns")
        print(f"auto-compact: at {' or '.join(triggers)}")
    else:
        print("auto-compact: off")
    if config.mcp_config and config.permission_mode == "default" and not config.allowed_tools:
        print("WARNING: an MCP config is set but IRIS_ALLOWED_TOOLS is empty under")
        print("  permission mode 'default'. The agent's tool calls will be SILENTLY")
        print("  skipped (it may even claim it acted). Allowlist the tools you want,")
        print("  e.g. IRIS_ALLOWED_TOOLS=mcp__memory__recall,mcp__memory__remember")
    has_jobs_server = _mcp_config_has_jobs_server(config.mcp_config)
    if has_jobs_server and not any(t.startswith("mcp__jobs__") for t in config.allowed_tools):
        print("WARNING: the mcp config has a 'jobs' server but no mcp__jobs__ tool is")
        print("  allowlisted, so the agent's job calls will be silently skipped.")
        print("  Add the five jobs tools to IRIS_ALLOWED_TOOLS:")
        print("  mcp__jobs__spawn_job, mcp__jobs__list_jobs, mcp__jobs__job_status,")
        print("  mcp__jobs__cancel_job, mcp__jobs__job_result")
    if config.jobs_enabled and not has_jobs_server:
        print("WARNING: IRIS_JOBS is on but the mcp config has no 'jobs' server, so")
        print("  the model cannot spawn jobs (only 'iris jobs spawn' can queue them).")
        print("  Add the jobs entry from examples/mcp.example.json to your mcp config.")
    if not config.allowed_user_ids:
        print("WARNING: IRIS_ALLOWED_USER_IDS is empty, so a network transport will")
        print("  answer ANYONE who can reach it (any DM sender, any group member).")
        print("  A personal subscription is single-user only; set it to your own id.")
    print("Run 'python -m iris chat' to talk to it, or 'python -m iris' for Discord.")
    return 0


def chat(config: Config) -> int:
    """A simple terminal REPL using the shared agent core."""
    agent = Agent.from_config(config)
    conversation_id = "cli:local"
    print("Iris terminal chat. Type 'exit' to quit, 'reset' to start fresh.\n")
    while True:
        try:
            prompt = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not prompt:
            continue
        if prompt.lower() in {"exit", "quit"}:
            return 0
        if prompt.lower() == "reset":
            agent.reset(conversation_id)
            print("(fresh conversation)\n")
            continue
        try:
            with _Spinner():
                result = agent.respond(conversation_id, prompt)
        except ClaudeError as exc:
            print(f"iris > [unavailable] {exc}\n")
            continue
        if result.is_error:
            print(f"iris > [error] {result.error}\n")
            continue
        print(f"iris > {result.text.strip()}\n")


def budget_tick(config: Config, *, now: float | None = None, sender=None) -> None:
    """Budget threshold pings and park expiry, ridden on the reminders tick.

    Clock-driven, therefore template-only by rule: pure file arithmetic and
    f-strings, no driver anywhere near this path. A failed send is not
    recorded, so the ping is retried on the next tick. ``sender`` is the
    notify-deliver test seam.
    """
    import time as _time

    from . import budget
    from .notify.deliver import send as notify_send

    now = _time.time() if now is None else now
    state = budget.BudgetState(config.budget_state)
    if 0 < state.park_until <= now:
        state.set_park_until(0.0)  # expired: the job runner reads the same state
    if config.monthly_credit <= 0 or not config.metrics_file:
        return
    records = budget.read_metrics(config.metrics_file, budget.window(now, "month"))
    spent = budget.summarize(records)["total_cost"]
    month = budget.month_key(now)
    crossed = budget.thresholds_crossed(spent, config.monthly_credit, state.pinged(month))
    if not crossed:
        return
    projected = budget.projection(records, now)
    for threshold in crossed:
        text = (f"budget: {threshold}% of the monthly agent credit used "
                f"(${spent:.2f} of ${config.monthly_credit:.2f}; "
                f"projecting ${projected:.2f} by month end)")
        if notify_send(text, token=config.discord_token,
                       channel=config.notify_channel, sender=sender):
            state.record_pings(month, [threshold])


def reminders_tick(config: Config, *, now: float | None = None, sender=None) -> int:
    """Deliver any reminders that are now due. Run from cron or a systemd timer."""
    import os

    from .reminders import ReminderStore, send_discord_message

    try:
        # Before the token guard: park expiry is pure file arithmetic and must
        # clear even on a host that only runs the tick for the budget check.
        budget_tick(config, now=now, sender=sender)
    except Exception:
        logging.getLogger("iris.cli").warning("budget tick failed", exc_info=True)
    if not config.discord_token:
        print("reminders-tick: IRIS_DISCORD_TOKEN is not set")
        return 1
    store = ReminderStore(os.environ.get("IRIS_REMINDERS_FILE", "iris-reminders.json"))
    due = store.pop_due()
    sent = 0
    for job in due:
        if send_discord_message(job["channel_id"], f"Reminder: {job['text']}", config.discord_token):
            sent += 1
        else:
            store.add(job["due_ts"], job["text"], job["channel_id"])  # re-queue on failure
    print(f"reminders-tick: {len(due)} due, {sent} delivered")
    return 0


def usage(config: Config, args, *, now: float | None = None) -> int:
    """Render the spend summary from the metrics file: pure file arithmetic.

    The credit/projection lines only make sense against the calendar month
    (the credit is monthly), so they render for the month period alone.
    """
    import json
    import time as _time
    from pathlib import Path

    from . import budget

    if not config.metrics_file:
        print("No metrics file is configured, so no spend is recorded.")
        print("Set IRIS_METRICS_FILE to a JSONL path; Iris logs one line per turn.")
        return 0
    if not Path(config.metrics_file).exists():
        print(f"No metrics recorded yet at {config.metrics_file}.")
        return 0
    now = _time.time() if now is None else now
    records = budget.read_metrics(config.metrics_file, budget.window(now, args.period))
    summary = budget.summarize(records)
    if args.as_json:
        print(json.dumps(summary))
        return 0
    credit = config.monthly_credit if args.period == "month" else 0.0
    proj = budget.projection(records, now) if credit > 0 else None
    print(budget.format_summary(summary, credit=credit, projection=proj))
    return 0


def _fmt_age(seconds: float) -> str:
    """Render an age like the jobs MCP server: whole hours past 60m, else minutes."""
    seconds = max(0, int(seconds))
    return f"{seconds // 3600}h" if seconds >= 3600 else f"{seconds // 60}m"


def jobs(config: Config, args) -> int:
    """Operate the background-job registry from the shell: pure file ops, no model.

    The bot's runner (and the jobs MCP server, for the agent) share the same
    registry file, so a job spawned here is claimed by a running bot within one
    watcher poll. Formatting mirrors the MCP server's friendly strings without
    importing it, so this surface works even without the mcp extra.
    """
    import time as _time

    from .jobs import JobStore

    store = JobStore(config.jobs_file)
    command = getattr(args, "jobs_command", None)

    if command == "spawn":
        prompt = " ".join(args.prompt).strip()
        if not prompt:
            print("usage: iris jobs spawn PROMPT... [--title T] [--model M] "
                  "[--timeout-minutes N] [--grants G]")
            return 2
        from .driver import DANGEROUS_BUILTINS

        grants = [g.strip() for g in (args.grants or "").split(",") if g.strip()]
        for name in grants:
            if name not in DANGEROUS_BUILTINS:
                print(f"Unknown grant {name!r}; valid grants: "
                      f"{', '.join(DANGEROUS_BUILTINS)}.")
                return 2
        minutes = int(args.timeout_minutes or 0)
        title = (args.title or "").strip() or prompt.splitlines()[0][:60]
        job_id = store.add(prompt, title, model=(args.model or "").strip(),
                           timeout_s=minutes * 60 if minutes > 0 else None,
                           grants=grants)
        print(f"Job #{job_id} queued: {title}")
        if not config.jobs_enabled:
            print("(note: IRIS_JOBS is off, so no runner will pick this up)")
        return 0

    if command == "list":
        wanted = (args.status or "").strip().lower() or None
        items = store.all(status=wanted)
        if not items:
            print(f"No {wanted} jobs." if wanted else "No jobs.")
            return 0
        now = _time.time()
        for job in reversed(items):  # all() sorts by id, so newest first
            stamp = job.get("started_at") or job.get("created_at") or now
            print(f"#{job['id']} [{job.get('status')} {_fmt_age(now - stamp)}] "
                  f"{job.get('title') or '(untitled)'}")
        return 0

    if command == "show":
        job = store.get(args.id)
        if job is None:
            print(f"No job #{args.id}.")
            return 1
        now = _time.time()
        print(f"Job #{job['id']}: {job.get('title') or '(untitled)'}")
        print(f"status: {job.get('status')}")
        for label, stamp in (("created", job.get("created_at")),
                             ("started", job.get("started_at")),
                             ("finished", job.get("finished_at"))):
            if stamp is not None:
                print(f"{label} {_fmt_age(now - float(stamp))} ago")
        if job.get("model"):
            print(f"model: {job['model']}")
        if job.get("grants"):
            print(f"grants: {', '.join(job['grants'])}")
        if job.get("cancel_requested"):
            print("cancel requested")
        result = job.get("result") or {}
        if job.get("status") == "failed" and result.get("error"):
            print(f"error: {result['error']}")
        elif (result.get("text") or "").strip():
            print(f"result:\n{result['text'].strip()}")
        return 0

    if command == "cancel":
        print(store.request_cancel(args.id))
        return 0

    print("usage: iris jobs {list,show,cancel,spawn} ...")
    return 2


def skills(config: Config) -> int:
    """List the skills the agent can use (and link IRIS_SKILLS_DIR if set)."""
    from .skills import discover, link_skills

    if config.skills_dir:
        made = link_skills(config.skills_dir)
        if made:
            print(f"Linked {made} skill(s) from {config.skills_dir}")
    found = discover()
    if not found:
        print("No skills found in ~/.claude/skills.")
        print("Set IRIS_SKILLS_DIR to a folder of SKILL.md skills, or drop them there.")
        return 0
    print("Skills the agent can use:")
    for name, desc in found:
        print(f"  {name}" + (f" — {desc}" if desc else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="iris", description="A chat agent on your Claude subscription.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("discord", help="run the Discord bot (default)")
    sub.add_parser("telegram", help="run the Telegram bot")
    sub.add_parser("tui", help="full-screen terminal UI")
    sub.add_parser("chat", help="plain terminal REPL")
    doctor_parser = sub.add_parser("doctor", help="check that claude is installed and signed in")
    doctor_parser.add_argument("--no-probe", action="store_true", help="skip the metered sign-in test call")
    sub.add_parser("skills", help="list the skills the agent can use")
    sub.add_parser("reminders-tick", help="deliver due reminders (run from cron/timer)")
    usage_parser = sub.add_parser("usage", help="spend summary from the metrics file (no model call)")
    usage_parser.add_argument("--period", choices=["day", "week", "month"], default="month")
    usage_parser.add_argument("--json", action="store_true", dest="as_json",
                              help="dump the summary dict as JSON")
    jobs_parser = sub.add_parser("jobs", help="inspect and queue background jobs")
    jobs_sub = jobs_parser.add_subparsers(dest="jobs_command")
    jobs_list = jobs_sub.add_parser("list", help="list jobs, newest first")
    jobs_list.add_argument("--status", default="",
                           help="filter: pending|running|done|failed|cancelled|interrupted")
    jobs_show = jobs_sub.add_parser("show", help="one job's full detail")
    jobs_show.add_argument("id", type=int)
    jobs_cancel = jobs_sub.add_parser("cancel", help="cancel a pending or running job")
    jobs_cancel.add_argument("id", type=int)
    jobs_spawn = jobs_sub.add_parser("spawn", help="queue a background job")
    jobs_spawn.add_argument("prompt", nargs="+", help="full instructions for the worker")
    jobs_spawn.add_argument("--title", default="", help="short label for listings")
    jobs_spawn.add_argument("--model", default="", help="model override for this job")
    jobs_spawn.add_argument("--timeout-minutes", type=int, default=0,
                            help="time budget in minutes (0 = default 30)")
    jobs_spawn.add_argument("--grants", default="",
                            help="comma-separated normally-denied tools, e.g. Task")
    watch_parser = sub.add_parser("watch", help="run a command and ping you when it finishes")
    watch_parser.add_argument("--name", default=None, help="label for the notification")
    watch_parser.add_argument("--always", action="store_true", help="ping even on a quick success")
    watch_parser.add_argument("--quiet", action="store_true", help="suppress the ping for this run")
    watch_parser.add_argument("argv", nargs=argparse.REMAINDER, help="-- then the command to run")
    args = parser.parse_args(argv)

    # Configure logging once here so every command (chat, tui, reminders-tick,
    # not just the network adapters) surfaces agent warnings.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config = Config.from_env()
    # Make any configured skills discoverable before a bot/chat run starts.
    if config.skills_dir:
        from .skills import link_skills
        link_skills(config.skills_dir)
    command = args.command or "discord"

    if command == "doctor":
        return doctor(config, probe=not getattr(args, "no_probe", False))
    if command == "chat":
        return chat(config)
    if command == "skills":
        return skills(config)
    if command == "reminders-tick":
        return reminders_tick(config)
    if command == "usage":
        return usage(config, args)
    if command == "jobs":
        return jobs(config, args)
    if command == "tui":
        from .tui import run as run_tui
        run_tui(config)
        return 0
    if command == "watch":
        from .notify.watch_cmd import watch as run_watch
        cmd = list(args.argv)
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        if not cmd:
            print("usage: iris watch [--name N] [--always] [--quiet] -- <command>")
            return 2
        return run_watch(cmd, config, name=args.name, force=args.always, quiet=args.quiet)
    if command == "telegram":
        from .telegram_adapter import run as run_telegram
        run_telegram(config)
        return 0
    # discord
    from .discord_adapter import run as run_discord
    run_discord(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
