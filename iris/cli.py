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


def reminders_tick(config: Config) -> int:
    """Deliver any reminders that are now due. Run from cron or a systemd timer."""
    import os

    from .reminders import ReminderStore, send_discord_message

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
    watch_parser = sub.add_parser("watch", help="run a command and ping you when it finishes")
    watch_parser.add_argument("--name", default=None, help="label for the notification")
    watch_parser.add_argument("--always", action="store_true", help="ping even on a quick success")
    watch_parser.add_argument("--quiet", action="store_true", help="suppress the ping for this run")
    watch_parser.add_argument("command", nargs=argparse.REMAINDER, help="-- then the command to run")
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
    # When the watch subcommand is parsed, its REMAINDER positional (also named
    # "command") overwrites the subparser dest in the namespace, so args.command
    # becomes a list instead of the string "watch".  Detect that case by type.
    command = "watch" if isinstance(args.command, list) else (args.command or "discord")

    if command == "doctor":
        return doctor(config, probe=not getattr(args, "no_probe", False))
    if command == "chat":
        return chat(config)
    if command == "skills":
        return skills(config)
    if command == "reminders-tick":
        return reminders_tick(config)
    if command == "tui":
        from .tui import run as run_tui
        run_tui(config)
        return 0
    if command == "watch":
        from .notify.watch_cmd import watch as run_watch
        cmd = list(args.command)
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
