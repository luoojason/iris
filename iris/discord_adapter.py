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
from .attachments import conversation_dir, describe, safe_filename
from .config import Config
from .driver import ClaudeError
from .textutil import chunk_text
from .transcribe import build_transcriber, transcribe_audio

log = logging.getLogger("iris.discord")

DISCORD_LIMIT = 2000
RESET_COMMANDS = {"!reset", "!forget", "!newchat"}


async def _save_attachments(attachments, base_dir: str, conversation_id: str) -> list[str]:
    """Download message attachments and return their absolute paths."""
    paths: list[str] = []
    if not attachments or not base_dir:
        return paths
    conv_dir = conversation_dir(base_dir, conversation_id)
    for att in list(attachments)[:5]:
        dest = conv_dir / safe_filename(getattr(att, "filename", None))
        try:
            await att.save(dest)
            paths.append(str(dest.resolve()))
        except Exception as exc:  # one bad attachment should not sink the turn
            log.warning("could not save attachment %s: %s", getattr(att, "filename", "?"), exc)
    return paths


def build_client(config: Config, agent: Agent):
    """Build (but do not start) the Discord client. Returns the client."""
    import discord  # lazy: only needed when actually running on Discord

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    transcriber = build_transcriber(config)  # None unless IRIS_VOICE is on

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

        attach_paths = await _save_attachments(message.attachments, config.attachments_dir, conversation_id)
        transcripts = await asyncio.to_thread(transcribe_audio, attach_paths, transcriber)
        prompt = describe(prompt, attach_paths, transcripts)
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

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    agent = Agent.from_config(config)
    client = build_client(config, agent)
    client.run(config.discord_token, log_handler=None)
