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


def should_handle(message, bot_user, config) -> bool:
    """Decide whether to answer a message. Pure, so it is unit-testable.

    A thread counts as belonging to its parent channel for the allowlist, and the
    bot auto-replies to every message inside a thread: each thread is a focused
    "project space" with its own session (the conversation id is the thread's own
    channel id), while the general channel still follows the mention rules.
    """
    author = message.author
    if getattr(author, "bot", False) or (bot_user and author.id == bot_user.id):
        return False
    if config.allowed_user_ids and str(author.id) not in config.allowed_user_ids:
        return False

    channel = message.channel
    parent_id = getattr(channel, "parent_id", None)  # only threads have a parent
    is_thread = parent_id is not None

    if config.allowed_channel_ids:
        channel_ids = {str(channel.id)}
        if is_thread:
            channel_ids.add(str(parent_id))  # allow threads of an allowed channel
        if channel_ids.isdisjoint(set(config.allowed_channel_ids)):
            return False

    is_dm = getattr(channel, "guild", None) is None
    mentioned = bool(bot_user and bot_user in getattr(message, "mentions", []))
    return is_dm or is_thread or mentioned or config.respond_without_mention


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
    try:
        import discord  # lazy: only needed when actually running on Discord
    except ImportError as exc:
        raise SystemExit(
            "Discord support needs the extra: pip install 'iris-agent[discord]'"
        ) from exc

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    transcriber = build_transcriber(config)  # None unless IRIS_VOICE is on

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
        if not should_handle(message, client.user, config):
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
                result = await asyncio.to_thread(
                    agent.respond, conversation_id, prompt, bool(attach_paths)
                )
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
    if not config.allowed_user_ids:
        log.warning(
            "IRIS_ALLOWED_USER_IDS is empty: this bot will answer ANYONE who can "
            "reach it (any DM, any allowed channel). A personal subscription is "
            "single-user only; set it to your id."
        )
    agent = Agent.from_config(config)
    client = build_client(config, agent)
    client.run(config.discord_token, log_handler=None)
