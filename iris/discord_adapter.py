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
import random
from typing import Optional

from .agent import Agent, LiveTurn
from .attachments import conversation_dir, describe, safe_filename
from .config import Config
from .conversation import ConversationRunner, LiveConversationRunner, Turn
from .driver import ClaudeError, ClaudeResult
from .textutil import chunk_text
from .transcribe import build_transcriber, transcribe_audio

log = logging.getLogger("iris.discord")

DISCORD_LIMIT = 2000
RESET_COMMANDS = {"!reset", "!forget", "!newchat"}

# Short, varied interim lines for a turn that runs long. Kept casual and in
# Iris's voice; one is picked at random so a slow stretch does not read like a
# canned bot.
_ACK_LINES = (
    "on it",
    "on it, one sec",
    "working on it",
    "give me a moment",
    "digging into this",
    "let me take a look",
)


def _ack_line() -> str:
    return random.choice(_ACK_LINES)


def _reply_text(result: ClaudeResult, *, placeholder: bool) -> Optional[str]:
    """Map a turn's result to the user-facing string, or None to stay silent.

    Shared by the one-shot and live paths so they speak with one voice. The
    primary reply uses a placeholder for an empty success ("(no response)"); a
    stray follow-up stays silent instead, since an empty trailing line reads as a
    glitch.
    """
    if result.is_error:
        log.warning("turn errored: %s", result.error)
        return (
            "Something went wrong on that one. "
            + (f"({result.error})" if result.error else "Try again in a moment.")
        )
    text = result.text.strip()
    if text:
        return text
    return "(no response)" if placeholder else None


class _LiveAdapterHandle:
    """Adapts :class:`iris.agent.LiveTurn` to the runner's text-level LiveHandle."""

    def __init__(self, live: LiveTurn) -> None:
        self._live = live

    async def begin(self) -> None:
        await self._live.begin()

    def is_open(self) -> bool:
        return self._live.is_open()

    async def inject(self, text: str) -> bool:
        return await self._live.inject(text)

    async def result(self) -> Optional[str]:
        try:
            result = await self._live.result()
        except ClaudeError as exc:
            log.error("claude unavailable: %s", exc)
            self._live.close()
            return f"I can't reach my brain right now: {exc}"
        return _reply_text(result, placeholder=True)

    async def aftermath(self) -> list[str]:
        try:
            strays = await self._live.aftermath()
        except Exception:
            log.warning("live aftermath failed", exc_info=True)
            self._live.close()
            return []
        return [m for m in (_reply_text(s, placeholder=False) for s in strays) if m]

    def close(self) -> None:
        self._live.close()


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

    # One runner per conversation serializes its turns and coalesces messages
    # that arrive while a turn is in flight, so the user can keep talking.
    runners: dict[str, ConversationRunner] = {}

    def _clean_content(message) -> str:
        text = message.content or ""
        if client.user:
            for token in (f"<@{client.user.id}>", f"<@!{client.user.id}>"):
                text = text.replace(token, "")
        return text.strip()

    def _runner_for(conversation_id: str, channel):
        runner = runners.get(conversation_id)
        if runner is not None:
            return runner

        async def send(text: str) -> None:
            for piece in chunk_text(text, DISCORD_LIMIT):
                await channel.send(piece)

        if config.live_interrupt and agent.stream_driver is not None:
            # Live interrupt: a mid-turn message redirects the running turn.
            def start_turn(prompt: str, has_attachments: bool) -> _LiveAdapterHandle:
                return _LiveAdapterHandle(agent.live_turn(conversation_id, prompt, has_attachments))

            runner = LiveConversationRunner(
                start_turn=start_turn,
                send=send,
                ack_line=_ack_line,
                typing=channel.typing,
                ack_delay=config.ack_delay,
            )
            runners[conversation_id] = runner
            return runner

        async def run_turn(prompt: str, has_attachments: bool) -> Optional[str]:
            try:
                result = await asyncio.to_thread(
                    agent.respond, conversation_id, prompt, has_attachments
                )
            except ClaudeError as exc:
                log.error("claude unavailable: %s", exc)
                return f"I can't reach my brain right now: {exc}"
            return _reply_text(result, placeholder=True)

        runner = ConversationRunner(
            run_turn=run_turn,
            send=send,
            ack_line=_ack_line,
            typing=channel.typing,
            ack_delay=config.ack_delay,
        )
        runners[conversation_id] = runner
        return runner

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
            runners.pop(conversation_id, None)  # drop any queued-but-unsent turns
            await message.channel.send("Started a fresh conversation.")
            return

        attach_paths = await _save_attachments(message.attachments, config.attachments_dir, conversation_id)
        transcripts = await asyncio.to_thread(transcribe_audio, attach_paths, transcriber)
        prompt = describe(prompt, attach_paths, transcripts)
        if not prompt:
            return

        async def receipt() -> None:
            # Confirm a mid-task message was seen; it folds into the next turn.
            try:
                await message.add_reaction("\N{EYES}")
            except Exception:
                log.debug("could not react to mid-task message", exc_info=True)

        runner = _runner_for(conversation_id, message.channel)
        runner.submit(Turn(text=prompt, has_attachments=bool(attach_paths), receipt=receipt))

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
