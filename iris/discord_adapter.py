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

from .config import Config
from .driver import ClaudeDriver, ClaudeError
from .sessions import SessionStore

log = logging.getLogger("iris.discord")

DISCORD_LIMIT = 2000
RESET_COMMANDS = {"!reset", "!forget", "!newchat"}


def _chunk(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    """Split a reply into Discord-sized pieces, preferring line boundaries."""
    text = text or ""
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks


def build_client(config: Config, driver: ClaudeDriver, store: SessionStore):
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
            store.clear(conversation_id)
            await message.channel.send("Started a fresh conversation.")
            return
        if not prompt:
            return

        session_id = store.get(conversation_id)
        try:
            async with message.channel.typing():
                result = await asyncio.to_thread(driver.run, prompt, session_id)
        except ClaudeError as exc:
            log.error("claude unavailable: %s", exc)
            await message.channel.send(f"I can't reach my brain right now: {exc}")
            return

        if result.session_id:
            store.set(conversation_id, result.session_id)

        if result.is_error:
            log.warning("turn errored: %s", result.error)
            await message.channel.send(
                "Something went wrong on that one. "
                + (f"({result.error})" if result.error else "Try again in a moment.")
            )
            return

        reply = result.text.strip() or "(no response)"
        for piece in _chunk(reply):
            await message.channel.send(piece)

    return client


def run(config: Optional[Config] = None) -> None:
    """Load config, wire everything up, and run the Discord bot."""
    config = config or Config.from_env()
    if not config.discord_token:
        raise SystemExit("IRIS_DISCORD_TOKEN is not set. See .env.example.")

    driver = ClaudeDriver(
        claude_bin=config.claude_bin,
        model=config.model,
        system_prompt_file=config.persona_file,
        mcp_config=config.mcp_config,
        permission_mode=config.permission_mode,
        allowed_tools=config.allowed_tools or None,
        disallowed_tools=config.disallowed_tools or None,
        add_dirs=config.add_dirs or None,
        timeout=config.turn_timeout,
    )
    store = SessionStore(config.session_store_path)
    client = build_client(config, driver, store)
    client.run(config.discord_token, log_handler=None)
