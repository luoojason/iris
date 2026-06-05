"""Command line surface: run the bot, chat in the terminal, or check setup."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys

from .agent import Agent
from .config import Config
from .driver import ClaudeError


def doctor(config: Config) -> int:
    """Verify the claude binary is present and signed in."""
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
    print(f"model: {config.model or '(claude default)'}")
    print(f"persona: {config.persona_file or '(none)'}")
    print(f"mcp tools: {config.mcp_config or '(none)'}")
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
            result = agent.respond(conversation_id, prompt)
        except ClaudeError as exc:
            print(f"iris > [unavailable] {exc}\n")
            continue
        if result.is_error:
            print(f"iris > [error] {result.error}\n")
            continue
        print(f"iris > {result.text.strip()}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="iris", description="A chat agent on your Claude subscription.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("discord", help="run the Discord bot (default)")
    sub.add_parser("telegram", help="run the Telegram bot")
    sub.add_parser("chat", help="talk to the agent in the terminal")
    sub.add_parser("doctor", help="check that claude is installed and signed in")
    args = parser.parse_args(argv)

    config = Config.from_env()
    command = args.command or "discord"

    if command == "doctor":
        return doctor(config)
    if command == "chat":
        return chat(config)
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
