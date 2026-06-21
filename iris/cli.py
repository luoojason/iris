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


def connection_doctor_lines(config) -> list[str]:
    import shutil as _shutil
    from .connections import ConnectionStore

    store = ConnectionStore(config.connections_file)
    conns = store.list()
    if not conns:
        return ["connections: none registered (iris mcp add NAME --command CMD)"]
    lines = [f"connections: {len(conns)} registered"]
    for c in conns:
        state = "on" if c.enabled else "off"
        lines.append(f"  [{state}] {c.name}: {c.command}")
        if c.enabled and _shutil.which(c.command) is None and not c.command.startswith("/"):
            lines.append(f"    WARNING: command not found on PATH: {c.command}")
        if c.enabled and not c.allowed_tools:
            lines.append(f"    WARNING: {c.name} has no allowed tools, so it is inert")
    return lines


def doctor(config: Config, probe: bool = True, fix: bool = False) -> int:
    """Verify the claude binary is present and actually signed in.

    With ``fix`` it also performs safe, mechanical repairs (no config changes):
    flips crashed job runners to failed so they stop holding a slot, and prunes
    old terminal jobs past IRIS_JOBS_KEEP.
    """
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
    from pathlib import Path

    if config.standing_orders_file:
        orders = Path(config.standing_orders_file)
        if not orders.exists():
            print(f"standing orders: MISSING file {config.standing_orders_file}")
        else:
            size = orders.stat().st_size
            print(f"standing orders: {config.standing_orders_file} ({size} bytes)")
            if size > 2048:
                print("WARNING: standing orders are over 2KB. Every byte is appended to")
                print("  the system prompt and re-billed on every turn; trim the file.")
    else:
        print("standing orders: (none)")
    print(f"mcp tools: {config.mcp_config or '(none)'}")
    print(f"allowed tools: {', '.join(config.allowed_tools) if config.allowed_tools else '(none)'}")
    for line in connection_doctor_lines(config):
        print(line)
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
    try:
        from .wakes import doctor_lines
        for line in doctor_lines(config):
            print(line)
    except Exception as exc:
        print(f"wakes: could not validate the rules file ({exc})")
    try:
        from .heartbeat import doctor_lines as heartbeat_doctor_lines
        for line in heartbeat_doctor_lines(config):
            print(line)
    except Exception as exc:
        print(f"heartbeat: could not validate the checks file ({exc})")
    if config.usage_budget_usd > 0:
        try:
            from .usage import UsageLedger, level_for, percent_used

            pct = percent_used(UsageLedger(config.usage_file).month(), config.usage_budget_usd)
            lvl = level_for(pct, config.usage_tighten_at, config.usage_park_at)
            print(f"credit guard: {pct:.0f}% of ${config.usage_budget_usd:.2f} used this month ({lvl})")
        except Exception as exc:
            print(f"credit guard: could not read the ledger ({exc})")
    if config.jobs_enabled:
        # A workspace that contains the agent's own state directory hands a
        # files-granted job the pen that writes the schedules (commands the
        # clock will run) and every other registry. Warn loudly.
        try:
            from .workspaces import WorkspaceStore

            state_dir = Path(config.schedules_file).resolve().parent
            for ws_name, ws_path in WorkspaceStore(config.workspaces_file).list().items():
                ws = Path(ws_path).resolve()
                if ws == state_dir or ws in state_dir.parents:
                    print(f"WARNING: workspace {ws_name!r} ({ws_path}) contains the agent's state")
                    print("  files (schedules, registries, .env). A files-granted job there can")
                    print("  rewrite them — including putting commands on the clock. Register a")
                    print("  narrower directory instead.")
        except Exception as exc:
            print(f"workspaces: could not check the registry ({exc})")
    if "browser" in config.job_grants:
        if shutil.which("npx"):
            print("browser grant: on (Playwright MCP via npx)")
        else:
            print("WARNING: 'browser' is in IRIS_JOB_GRANTS but npx is not on PATH,")
            print("  so the Playwright MCP server cannot launch. Install Node.js, or")
            print("  point IRIS_BROWSER_MCP_CMD at a working launch command.")
    if config.auto_resume:
        if config.home_channel:
            print(f"auto-resume: on (home channel {config.home_channel}, "
                  f"max {config.auto_resume_max_per_day}/day, dropped when parked)")
        else:
            print("WARNING: IRIS_AUTO_RESUME is on but IRIS_DISCORD_HOME_CHANNEL is")
            print("  empty, so a finished background task has nowhere to resume and")
            print("  auto-resume will silently do nothing. Set the home channel.")
    if config.goals_enabled:
        from .goals import GoalStore
        active = len(GoalStore(config.goals_file).active())
        print(f"goals: on ({active} active, judge {config.goal_judge_model}, "
              f"gated on weekly usage < {config.proactive_usage_max:.0f}%)")
        if not config.home_channel:
            print("  NOTE: IRIS_DISCORD_HOME_CHANNEL is empty; a goal set outside a "
                  "thread has nowhere to report.")
    if config.job_verify_enabled and not config.jobs_enabled:
        print("WARNING: IRIS_JOB_VERIFY is on but IRIS_JOBS is off, so no job ever runs")
        print("  and the verification gate never fires. Enable IRIS_JOBS or turn it off.")
    if (config.goals_enabled or config.proactive_enabled) and config.usage_budget_usd <= 0:
        print("NOTE: self-started work is on (IRIS_GOALS/IRIS_PROACTIVE) but")
        print("  IRIS_USAGE_BUDGET_USD is 0, so the credit-guard park backstop is")
        print("  disarmed. The weekly-usage gate still bounds it; set a budget to arm park.")
    if config.webhook_enabled:
        if not config.webhook_token:
            print("WARNING: IRIS_WEBHOOK is on but IRIS_WEBHOOK_TOKEN is empty; the")
            print("  listener will refuse to start (an unauthenticated webhook is unsafe).")
        elif config.webhook_bind in ("0.0.0.0", "::"):
            print(f"WARNING: the webhook listener binds {config.webhook_bind} (all")
            print("  interfaces). Bind 127.0.0.1 or a tailnet address unless you intend")
            print("  to expose it; it is token-checked but still an inbound surface.")
        else:
            print(f"webhook: on ({config.webhook_bind}:{config.webhook_port}, token set)")
    if config.mcp_config and config.permission_mode == "default" and not config.allowed_tools:
        print("WARNING: an MCP config is set but IRIS_ALLOWED_TOOLS is empty under")
        print("  permission mode 'default'. The agent's tool calls will be SILENTLY")
        print("  skipped (it may even claim it acted). Allowlist the tools you want,")
        print("  e.g. IRIS_ALLOWED_TOOLS=mcp__memory__recall,mcp__memory__remember")
    if not config.allowed_user_ids:
        print("WARNING: IRIS_ALLOWED_USER_IDS is empty, so a network transport will")
        print("  answer ANYONE who can reach it (any DM sender, any group member).")
        print("  A personal subscription is single-user only; set it to your own id.")
    if fix:
        if config.jobs_enabled:
            from .jobs import JobStore, repair_dead_runners
            store = JobStore(config.jobs_file, keep=config.jobs_keep)
            repaired = repair_dead_runners(store)
            pruned = store.prune(config.jobs_keep)
            print(f"fix: repaired {repaired} dead job runner(s), pruned {pruned} old job(s).")
        else:
            print("fix: nothing to repair (background jobs are off).")
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


def reminders_tick(config: Config) -> int:
    """Deliver any reminders that are now due. Run from cron or a systemd timer."""
    import os

    from . import reminders as rmod

    # Reminder delivery needs the bot token (a REST post), but the budget,
    # wakes, and schedules ticks below do not all need it (a schedule launch is
    # token-free), so a missing token skips only delivery, not the whole tick.
    if config.discord_token:
        store = rmod.ReminderStore(os.environ.get("IRIS_REMINDERS_FILE", "iris-reminders.json"))
        due = store.pop_due()
        sent = 0
        for job in due:
            if rmod.send_discord_message(job["channel_id"], rmod.render_reminder(job), config.discord_token):
                sent += 1
            else:
                store.requeue(job)  # retried on the next tick
        print(f"reminders-tick: {len(due)} due, {sent} delivered")
    else:
        print("reminders-tick: IRIS_DISCORD_TOKEN not set; skipping reminder delivery")
    # The budget check and the wake rules ride the same tick. Neither may
    # ever take reminder delivery down with it, so both are fail-soft to a
    # printed line, and neither makes a model call.
    try:
        from .usage import budget_tick
        print(budget_tick(config))
    except Exception as exc:
        print(f"budget tick failed: {exc}")
    try:
        from .wakes import tick_wakes
        print(tick_wakes(config))
    except Exception as exc:
        print(f"wakes tick failed: {exc}")
    try:
        from .schedules import tick_schedules
        print(tick_schedules(config))
    except Exception as exc:
        print(f"schedules tick failed: {exc}")
    try:
        from .heartbeat import tick_heartbeat
        print(tick_heartbeat(config))
    except Exception as exc:
        print(f"heartbeat tick failed: {exc}")
    if config.jobs_enabled:
        try:
            from .jobs import notify_dead_jobs
            print(f"dead-jobs: {notify_dead_jobs(config)} notified")
        except Exception as exc:
            print(f"dead-jobs sweep failed: {exc}")
    return 0


def usage_cmd(config: Config) -> int:
    """Print this month's credit draw (a report, not a check; always exits 0)."""
    from .usage import summary_text

    print(summary_text(config))
    return 0


def trace_cmd(config: Config, *, days: int = 7, as_json: bool = False, now=None) -> int:
    """Digest the trace ledger over a window: runs, errors by category, cost, latency.

    Model-free, so a scheduled owner-authored job can render and deliver it through
    the notify spine. ``now`` is injectable for tests.
    """
    import json as _json
    import time as _time

    from .trace import load_traces, render_digest, summarize_traces

    if not config.trace_file:
        print("No trace ledger configured (set IRIS_TRACE_FILE).")
        return 0
    now = _time.time() if now is None else now
    since = now - days * 86400
    summary = summarize_traces(load_traces(config.trace_file, since_ts=since))
    print(_json.dumps(summary, indent=2) if as_json else render_digest(summary, days=days))
    return 0


def digest_cmd(config: Config, *, days: int = 1, now=None) -> int:
    """Print a recap of the last ``days`` of conversations (one summary turn)."""
    import time as _time

    from .digest import build_digest

    now = _time.time() if now is None else now
    text = build_digest(config, now=now, days=days)
    print(text or "Nothing substantive to recap.")
    return 0


def audit_cmd(config: Config, *, as_json: bool = False) -> int:
    """Security/compliance self-audit. Exit 2 if any critical/high finding, else 0,
    so it works as a cron/CI tripwire. Model-free and read-only."""
    import json as _json

    from .audit import render_audit, run_audit, worst_severity

    findings = run_audit(config)
    if as_json:
        print(_json.dumps([f.__dict__ for f in findings], indent=2))
    else:
        print(render_audit(findings))
    return 2 if worst_severity(findings) in ("critical", "high") else 0


def workspaces_cmd(config: Config, action: str, name: str = "", path: str = "") -> int:
    """Owner-side registry of directories jobs may work in (names, not paths)."""
    from .workspaces import WorkspaceStore

    store = WorkspaceStore(config.workspaces_file)
    if action == "add":
        try:
            resolved = store.add(name, path)
        except ValueError as exc:
            print(f"workspaces add: {exc}")
            return 2
        print(f"workspace {name} -> {resolved}")
        return 0
    if action == "remove":
        if store.remove(name):
            print(f"removed workspace {name}")
            return 0
        print(f"no workspace named {name}")
        return 1
    items = store.list()
    if not items:
        print("No workspaces registered. Add one with: iris workspaces add <name> <path>")
        return 0
    for ws_name, ws_path in items.items():
        print(f"{ws_name} -> {ws_path}")
    return 0


def schedule_cmd(config: Config, args) -> int:
    """Owner-side authoring of scheduled jobs (the clock may start these)."""
    from .schedules import ScheduleStore, add_rule, describe_rule

    store = ScheduleStore(config.schedules_file)
    action = getattr(args, "schedule_action", None) or "list"
    if action == "add":
        try:
            rule = add_rule(
                store,
                title=args.title,
                when=args.at,
                every=args.every,
                instructions=args.instructions,
                command=getattr(args, "script_command", ""),
                grants=args.grant,
                workspace=args.workspace,
                cap=args.cap,
                default_cap=config.schedule_monthly_cap,
            )
        except ValueError as exc:
            print(f"schedule add: {exc}")
            return 2
        print(f"Recorded schedule {describe_rule(rule)}")
        if not config.scheduled_jobs_enabled:
            print("Note: IRIS_SCHEDULED_JOBS is not set, so this rule is inert "
                  "until you enable it (and restart the reminders timer).")
        return 0
    if action == "remove":
        if store.remove(args.rule_id):
            print(f"Removed schedule #{args.rule_id}.")
            return 0
        print(f"No schedule #{args.rule_id}.")
        return 1
    rules = store.all()
    if not rules:
        print("No schedules recorded. Add one with: iris schedule add "
              "--title <t> --at <when> [--every 1d] --instructions <prompt>")
        return 0
    for rule in rules:
        print(describe_rule(rule))
    if not config.scheduled_jobs_enabled:
        print("(IRIS_SCHEDULED_JOBS is off: nothing fires.)")
    return 0


def heartbeat_cmd(config: Config) -> int:
    """Show the current status of the health checklist (read-only; no ping, no model)."""
    import time
    from pathlib import Path

    from .heartbeat import _evaluate, load_checks, validate_checks

    path = Path(config.heartbeat_file)
    if not path.exists():
        print("No heartbeat checks. Author IRIS_HEARTBEAT_FILE with a JSON list of "
              "checks (disk_free, file_fresh, url_ok) to get a silent-by-default "
              "health watch.")
        return 0
    checks, problem = load_checks(path)
    if problem:
        print(f"heartbeat: {problem}")
        return 1
    now = time.time()
    for check in checks:
        name = check.get("name") if isinstance(check, dict) else "check"
        problems = validate_checks([check])
        if problems:
            print(f"{name}: invalid ({problems[0]})")
            continue
        ok, detail = _evaluate(check, now=now, http_timeout=config.heartbeat_http_timeout)
        print(f"{name}: ok" if ok else f"{name}: FAIL: {detail}")
    return 0


def goals_cmd(config: Config, args) -> int:
    """Owner-side view and steering of the standing goals the tick advances."""
    import time

    from .goals import GoalStore

    store = GoalStore(config.goals_file)
    action = getattr(args, "goals_action", None) or "list"
    if action == "cancel":
        goal = store.get(args.goal_id)
        if goal is None:
            print(f"No goal #{args.goal_id}.")
            return 1
        store.transition(args.goal_id, "cancelled", time.time())
        print(f"Cancelled goal #{args.goal_id}: {goal['text']}")
        return 0
    goals = store.all()
    if not goals:
        print("No goals set. Iris records them when you give her one to pursue.")
        return 0
    for g in goals:
        if g.get("status") == "active":
            print(f"#{g['id']} [active {g.get('steps', 0)}/{g.get('max_steps', '?')}]: {g['text']}")
        else:
            print(f"#{g['id']} [{g.get('status')}]: {g['text']}")
    if not config.goals_enabled:
        print("(IRIS_GOALS is off: nothing advances.)")
    return 0


def skills_cmd(config: Config, args) -> int:
    """Owner-side review and approval of Iris's proposed changes to her own skills.

    A proposal is staged by the model (propose_skill / the maintain review) but
    only becomes live behavior here, on an explicit approve: self-modification is
    always owner-gated.
    """
    import time

    from .skills import SkillProposalStore, apply_proposal

    store = SkillProposalStore(config.skill_proposals_file)
    action = getattr(args, "skills_action", None)

    if action == "show":
        p = store.get(args.proposal_id)
        if p is None:
            print(f"No skill proposal #{args.proposal_id}.")
            return 1
        print(f"Proposal #{p['id']} [{p.get('status')}] {p.get('kind')} '{p['name']}'")
        print(f"rationale: {(p.get('rationale') or '').strip()}")
        print("--- SKILL.md ---")
        print(p.get("content", ""))
        return 0

    if action == "approve":
        p = store.get(args.proposal_id)
        if p is None:
            print(f"No skill proposal #{args.proposal_id}.")
            return 1
        if p.get("status") != "pending":
            print(f"Skill proposal #{p['id']} was already {p.get('status')}; "
                  "nothing to approve.")
            return 2
        if not config.skills_dir:
            print("Set IRIS_SKILLS_DIR first: that's the directory an approved skill "
                  "is written into and linked from.")
            return 2
        try:
            path = apply_proposal(p, config.skills_dir)
        except ValueError as exc:
            print(f"skills approve: {exc}")
            return 2
        store.transition(args.proposal_id, "approved", time.time())
        print(f"Approved skill #{p['id']} ('{p['name']}') -> {path}. It is now live.")
        return 0

    if action == "reject":
        if store.transition(args.proposal_id, "rejected", time.time()) is None:
            print(f"No skill proposal #{args.proposal_id}.")
            return 1
        print(f"Rejected skill proposal #{args.proposal_id}.")
        return 0

    # "pending" (and any unrecognized action) -> list staged proposals
    items = store.pending()
    if not items:
        print("No pending skill proposals. Iris stages them here when she proposes a "
              "change to her own skills; review with 'iris skills show <id>'.")
        return 0
    for p in items:
        print(f"#{p['id']} {p.get('kind')} '{p['name']}': {(p.get('rationale') or '').strip()[:140]}")
    print("Approve with 'iris skills approve <id>' (or reject <id>); show <id> for the full text.")
    return 0


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


def _parse_kv(pairs):
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise SystemExit(f"bad --env {p!r}: expected K=V")
        k, _, v = p.partition("=")
        out[k.strip()] = v
    return out


def mcp_command(args, config) -> int:
    """Owner CLI for MCP connections. Returns a process exit code."""
    from .connections import ConnectionStore

    store = ConnectionStore(config.connections_file)
    action = args.mcp_action

    if action == "add":
        allow = list(args.allow or [])
        if args.allow_all:
            allow.append(f"mcp__{args.name}")
        try:
            store.add(
                args.name, args.command,
                args=args.arg or [], env=_parse_kv(args.env),
                allowed_tools=allow,
            )
        except ValueError as exc:
            print(f"error: {exc}")
            return 1
        print(f"added connection {args.name!r} (enabled). Allowed tools: {allow or '(none yet — add with --allow)'}")
        return 0

    if action == "list":
        conns = store.list()
        if args.json:
            import json as _json
            print(_json.dumps([
                {"name": c.name, "command": c.command, "args": c.args,
                 "enabled": c.enabled, "allowed_tools": c.allowed_tools,
                 "env_keys": sorted(c.env)} for c in conns
            ], indent=2))
            return 0
        if not conns:
            print("no connections. Add one with: iris mcp add NAME --command CMD")
            return 0
        for c in conns:
            state = "on " if c.enabled else "off"
            tools = ", ".join(c.allowed_tools) or "(no tools allowed)"
            # env values are intentionally never printed (secrets); only command/args/tools
            print(f"[{state}] {c.name}: {c.command} {' '.join(c.args)}  ->  {tools}")
        return 0

    if action == "remove":
        ok = store.remove(args.name)
        print(f"removed {args.name!r}" if ok else f"no connection named {args.name!r}")
        return 0 if ok else 1

    if action in ("enable", "disable"):
        try:
            store.set_enabled(args.name, action == "enable")
        except ValueError as exc:
            print(f"error: {exc}")
            return 1
        print(f"{action}d {args.name!r}")
        return 0

    if action == "import":
        import json as _json
        try:
            with open(args.path, encoding="utf-8") as _f:
                data = _json.loads(_f.read())
        except (OSError, ValueError) as exc:
            print(f"error: cannot read {args.path}: {exc}")
            return 1
        servers = (data or {}).get("mcpServers", {})
        if not servers:
            print("no mcpServers found in that file")
            return 1
        added = 0
        for name, spec in servers.items():
            if store.get(name) is not None:
                print(f"skip {name!r}: already exists")
                continue
            try:
                store.add(
                    name, str(spec.get("command", "")),
                    args=[str(a) for a in spec.get("args", [])],
                    env={str(k): str(v) for k, v in (spec.get("env") or {}).items()},
                    allowed_tools=[], enabled=False,
                )
                added += 1
            except ValueError as exc:
                print(f"skip {name!r}: {exc}")
        print(f"imported {added} connection(s), disabled. Enable + allow tools, e.g.: iris mcp enable NAME")
        return 0

    if action == "test":
        conn = store.get(args.name)
        if conn is None:
            print(f"no connection named {args.name!r}")
            return 1
        from .mcp_probe import ProbeError, probe_tools
        try:
            tools = probe_tools(conn.command, conn.args, conn.env)
        except ProbeError as exc:
            print(f"could not probe {args.name!r}: {exc}")
            return 1
        if not tools:
            print(f"{args.name!r} started but exposed no tools")
            return 0
        print(f"{args.name!r} exposes: " + ", ".join(f"mcp__{args.name}__{t}" for t in tools))
        print("allow them with: iris mcp add (or re-add) using --allow <tool>")
        return 0

    print("unknown mcp action")
    return 1


def build_parser() -> argparse.ArgumentParser:
    """Build and return the top-level iris argument parser."""
    parser = argparse.ArgumentParser(prog="iris", description="A chat agent on your Claude subscription.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("discord", help="run the Discord bot (default)")
    sub.add_parser("telegram", help="run the Telegram bot")
    sub.add_parser("tui", help="full-screen terminal UI")
    sub.add_parser("chat", help="plain terminal REPL")
    doctor_parser = sub.add_parser("doctor", help="check that claude is installed and signed in")
    doctor_parser.add_argument("--no-probe", action="store_true", help="skip the metered sign-in test call")
    doctor_parser.add_argument("--fix", action="store_true", help="also do safe mechanical repairs (dead job runners, prune old jobs)")
    skills_parser = sub.add_parser("skills", help="list skills; review/approve Iris's proposed skill changes")
    skills_sub = skills_parser.add_subparsers(dest="skills_action")
    skills_sub.add_parser("pending", help="list staged skill proposals")
    for act in ("show", "approve", "reject"):
        p = skills_sub.add_parser(act, help=f"{act} a skill proposal by id")
        p.add_argument("proposal_id", type=int)
    sub.add_parser("reminders-tick", help="deliver due reminders (run from cron/timer)")
    pro_parser = sub.add_parser("proactive-tick", help="run a proactive review (assist|maintain) from cron; gated on weekly usage")
    pro_parser.add_argument("kind", choices=["assist", "maintain"], help="which review to run")
    sub.add_parser("goal-tick", help="advance one active goal a step (run from cron; gated on weekly usage)")
    goals_parser = sub.add_parser("goals", help="see and steer your standing goals")
    goals_sub = goals_parser.add_subparsers(dest="goals_action")
    goals_sub.add_parser("list", help="list goals (default)")
    g_cancel = goals_sub.add_parser("cancel", help="cancel a goal by id")
    g_cancel.add_argument("goal_id", type=int)
    sub.add_parser("usage", help="show this month's credit draw and budget level")
    trace_parser = sub.add_parser("trace", help="digest the trace ledger: runs, errors, cost over a window")
    trace_parser.add_argument("--days", type=int, default=7, help="how many days back to summarize (default 7)")
    trace_parser.add_argument("--json", action="store_true", help="emit the raw summary as JSON")
    audit_parser = sub.add_parser("audit", help="security/compliance self-audit (exit 2 on critical/high)")
    audit_parser.add_argument("--json", action="store_true", help="emit findings as JSON")
    digest_parser = sub.add_parser("digest", help="recap the last day's conversations (one summary turn)")
    digest_parser.add_argument("--days", type=int, default=1, help="how many days back to recap (default 1)")
    sub.add_parser("heartbeat", help="show the current status of your health checklist")
    sub.add_parser("webhook", help="run the inbound webhook-wake listener (own process)")
    job_run_parser = sub.add_parser("job-run", help="run a recorded background job (internal; spawned by the jobs tool)")
    job_run_parser.add_argument("job_id", type=int)
    jobs_parser = sub.add_parser("jobs", help="the terminal job console: see and steer background jobs")
    jobs_parser.add_argument("--tui", action="store_true", help="open the full-screen jobs view")
    jobs_sub = jobs_parser.add_subparsers(dest="jobs_action")
    jobs_sub.add_parser("list", help="list jobs (default)")
    js_show = jobs_sub.add_parser("show", help="show one job in full")
    js_show.add_argument("job_id", type=int)
    js_run = jobs_sub.add_parser("run", help="create and launch a job from the terminal")
    js_run.add_argument("--title", required=True)
    js_run.add_argument("--instructions", required=True)
    js_run.add_argument("--grant", default="", help="extra grants, comma-separated: shell,files")
    js_run.add_argument("--workspace", default="", help="a registered workspace name")
    for act in ("cancel", "resume", "rerun", "artifacts", "deliver"):
        p = jobs_sub.add_parser(act, help=f"{act} a job by id")
        p.add_argument("job_id", type=int)
    js_prune = jobs_sub.add_parser("prune", help="drop old terminal jobs")
    js_prune.add_argument("--keep", type=int, default=None)
    sched_parser = sub.add_parser("schedule", help="owner-authored scheduled jobs (the clock may start these)")
    sched_sub = sched_parser.add_subparsers(dest="schedule_action")
    sc_add = sched_sub.add_parser("add", help="record a schedule rule")
    sc_add.add_argument("--title", required=True)
    sc_add.add_argument("--at", required=True, help="first firing: +30m, +2h, +1d, or an ISO datetime (UTC)")
    sc_add.add_argument("--every", default="", help="recurrence: every 30m / 2h / 1d (omit for one-shot)")
    sc_add.add_argument("--instructions", default="", help="the job prompt (a job rule)")
    # dest must NOT be the auto-derived "command": that collides with the
    # top-level subparsers' dest="command" and clobbers the subcommand name,
    # routing `schedule add --command ...` to the default (Discord) runner.
    sc_add.add_argument("--command", dest="script_command", default="",
                        help="a shell command instead (a script rule, zero model calls)")
    sc_add.add_argument("--grant", default="", help="job grants, comma-separated: shell,files")
    sc_add.add_argument("--workspace", default="", help="a registered workspace name")
    sc_add.add_argument("--cap", type=int, default=None, help="monthly fire cap (default IRIS_SCHEDULE_MONTHLY_CAP)")
    sc_remove = sched_sub.add_parser("remove", help="remove a schedule rule")
    sc_remove.add_argument("rule_id", type=int)
    sched_sub.add_parser("list", help="list schedule rules (default)")
    ws_parser = sub.add_parser("workspaces", help="manage the directories jobs may work in")
    ws_sub = ws_parser.add_subparsers(dest="ws_action")
    ws_add = ws_sub.add_parser("add", help="register a directory under a name")
    ws_add.add_argument("name")
    ws_add.add_argument("path")
    ws_remove = ws_sub.add_parser("remove", help="unregister a workspace")
    ws_remove.add_argument("name")
    ws_sub.add_parser("list", help="list registered workspaces")
    watch_parser = sub.add_parser("watch", help="run a command and ping you when it finishes")
    watch_parser.add_argument("--name", default=None, help="label for the notification")
    watch_parser.add_argument("--always", action="store_true", help="ping even on a quick success")
    watch_parser.add_argument("--quiet", action="store_true", help="suppress the ping for this run")
    watch_parser.add_argument("--fold", action="store_true", help="also fold the completion into Iris's next turn")
    watch_parser.add_argument("--resume", action="store_true", help="enqueue a follow-up turn so Iris continues the chain (needs IRIS_AUTO_RESUME)")
    watch_parser.add_argument("argv", nargs=argparse.REMAINDER, help="-- then the command to run")
    mcp_p = sub.add_parser("mcp", help="connect your own MCP servers")
    mcp_sub = mcp_p.add_subparsers(dest="mcp_action", required=True)

    p_add = mcp_sub.add_parser("add", help="register an MCP server connection")
    p_add.add_argument("name")
    p_add.add_argument("--command", required=True)
    p_add.add_argument("--arg", action="append", help="a command argument (repeatable)")
    p_add.add_argument("--env", action="append", help="K=V env var (repeatable)")
    p_add.add_argument("--allow", action="append", help="an allowed tool name (repeatable)")
    p_add.add_argument("--allow-all", action="store_true", help="allow the whole server (mcp__NAME)")

    p_list = mcp_sub.add_parser("list", help="list connections")
    p_list.add_argument("--json", action="store_true")

    for verb in ("remove", "enable", "disable"):
        pv = mcp_sub.add_parser(verb, help=f"{verb} a connection")
        pv.add_argument("name")

    p_imp = mcp_sub.add_parser("import", help="import servers from an existing mcp.json")
    p_imp.add_argument("path")

    p_test = mcp_sub.add_parser("test", help="probe a connection and list its tools")
    p_test.add_argument("name")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Configure logging once here so every command (chat, tui, reminders-tick,
    # not just the network adapters) surfaces agent warnings.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    command = args.command or "discord"

    # Usage errors must exit before Config.from_env() reads .env into the
    # process environment; a malformed invocation should have no side effects.
    watch_cmd: list[str] = []
    if command == "watch":
        watch_cmd = list(args.argv)
        if watch_cmd and watch_cmd[0] == "--":
            watch_cmd = watch_cmd[1:]
        if not watch_cmd:
            print("usage: iris watch [--name N] [--always] [--quiet] -- <command>")
            return 2
    if command == "workspaces" and not getattr(args, "ws_action", None):
        print("usage: iris workspaces {add <name> <path> | remove <name> | list}")
        return 2

    config = Config.from_env()
    # Make any configured skills discoverable before a bot/chat run starts.
    if config.skills_dir:
        from .skills import link_skills
        link_skills(config.skills_dir)

    if command == "doctor":
        return doctor(config, probe=not getattr(args, "no_probe", False),
                      fix=getattr(args, "fix", False))
    if command == "chat":
        return chat(config)
    if command == "skills":
        if getattr(args, "skills_action", None):
            return skills_cmd(config, args)
        return skills(config)
    if command == "reminders-tick":
        return reminders_tick(config)
    if command == "proactive-tick":
        import time as _time
        from .proactive import run_proactive_tick
        print(f"proactive-tick {args.kind}: {run_proactive_tick(config, args.kind, now=_time.time())}")
        return 0
    if command == "goal-tick":
        import time as _time
        from .goals import run_goal_tick
        print(f"goal-tick: {run_goal_tick(config, now=_time.time())}")
        return 0
    if command == "goals":
        return goals_cmd(config, args)
    if command == "usage":
        return usage_cmd(config)
    if command == "trace":
        return trace_cmd(config, days=args.days, as_json=args.json)
    if command == "audit":
        return audit_cmd(config, as_json=args.json)
    if command == "digest":
        return digest_cmd(config, days=args.days)
    if command == "heartbeat":
        return heartbeat_cmd(config)
    if command == "webhook":
        from .webhooks import run_webhook_server
        return run_webhook_server(config)
    if command == "schedule":
        return schedule_cmd(config, args)
    if command == "workspaces":
        return workspaces_cmd(
            config, args.ws_action,
            name=getattr(args, "name", ""), path=getattr(args, "path", ""),
        )
    if command == "mcp":
        return mcp_command(args, config)
    if command == "job-run":
        if not config.jobs_enabled:
            print("job-run: background jobs are disabled (set IRIS_JOBS=true)")
            return 1
        from . import jobs as jobs_mod
        return jobs_mod.run_job(args.job_id, config)
    if command == "jobs":
        if getattr(args, "tui", False) and not getattr(args, "jobs_action", None):
            from .jobs_tui import run as run_jobs_tui
            return run_jobs_tui(config)
        # default to the list view when no subcommand is given
        if not getattr(args, "jobs_action", None):
            args.jobs_action = "list"
        from .jobs_console import jobs_command
        return jobs_command(config, args)
    if command == "tui":
        from .tui import run as run_tui
        run_tui(config)
        return 0
    if command == "watch":
        from .notify.watch_cmd import watch as run_watch
        return run_watch(watch_cmd, config, name=args.name, force=args.always,
                         quiet=args.quiet, fold=getattr(args, "fold", False),
                         resume=getattr(args, "resume", False))
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
