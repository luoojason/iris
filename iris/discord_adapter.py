"""Discord front end.

Discord is just a transport here. A message comes in, it is handed to the
``claude`` brain through the driver with this channel's session, and the reply
is sent back. The same shape works for any chat platform; Discord is first
because it is what most personal agents target.

``discord.py`` is imported lazily so the core package and its tests do not
depend on it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .agent import Agent
from .config import Config
from .driver import ClaudeError
from .textutil import chunk_text

log = logging.getLogger("iris.discord")

DISCORD_LIMIT = 2000
RESET_COMMANDS = {"!reset", "!forget", "!newchat"}


def build_client(config: Config, agent: Agent):
    """Build (but do not start) the Discord client. Returns the client."""
    import discord  # lazy: only needed when actually running on Discord

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)

    def _should_handle(message) -> bool:
        if message.author.bot or (client.user and message.author.id == client.user.id):
            return False
        if config.allowed_user_ids and str(message.author.id) not in config.allowed_user_ids:
            return False
        if config.allowed_channel_ids and str(message.channel.id) not in config.allowed_channel_ids:
            return False
        is_dm = getattr(message.channel, "guild", None) is None
        mentioned = client.user in message.mentions if client.user else False
        return is_dm or mentioned or config.respond_without_mention

    def _clean_content(message) -> str:
        text = message.content or ""
        if client.user:
            for token in (f"<@{client.user.id}>", f"<@!{client.user.id}>"):
                text = text.replace(token, "")
        return text.strip()

    @client.event
    async def on_ready():
        log.info("Connected to Discord as %s", client.user)

    @client.event
    async def on_message(message):
        if not _should_handle(message):
            return

        conversation_id = f"discord:{message.channel.id}"
        prompt = _clean_content(message)

        if prompt in RESET_COMMANDS:
            agent.reset(conversation_id)
            await message.channel.send("Started a fresh conversation.")
            return
        if not prompt:
            return

        try:
            async with message.channel.typing():
                result = await asyncio.to_thread(agent.respond, conversation_id, prompt)
        except ClaudeError as exc:
            log.error("claude unavailable: %s", exc)
            await message.channel.send(f"I can't reach my brain right now: {exc}")
            return

        if result.is_error:
            log.warning("turn errored: %s", result.error)
            await message.channel.send(
                "Something went wrong on that one. "
                + (f"({result.error})" if result.error else "Try again in a moment.")
            )
            return

        reply = result.text.strip() or "(no response)"
        for piece in chunk_text(reply, DISCORD_LIMIT):
            await message.channel.send(piece)

    return client


def run(config: Optional[Config] = None) -> None:
    """Load config, wire everything up, and run the Discord bot."""
    config = config or Config.from_env()
    if not config.discord_token:
        raise SystemExit("IRIS_DISCORD_TOKEN is not set. See .env.example.")

    agent = Agent.from_config(config)
    client = build_client(config, agent)
    client.run(config.discord_token, log_handler=None)
