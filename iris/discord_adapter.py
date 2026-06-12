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
import time
from typing import Optional

from . import commands
from .agent import Agent, LiveTurn
from .attachments import conversation_dir, describe, safe_filename
from .config import Config
from .conversation import ConversationRunner, LiveConversationRunner, Turn
from .driver import ClaudeError, ClaudeResult
from .textutil import chunk_text
from .transcribe import build_transcriber, transcribe_audio

log = logging.getLogger("iris.discord")

DISCORD_LIMIT = 2000

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


def parse_conversation_channel(conversation_id) -> Optional[int]:
    """The numeric channel id behind a conversation id (``discord:<id>``), or None.

    Pure so the autonomous-resume path is unit-testable without the discord SDK.
    """
    if not conversation_id:
        return None
    raw = conversation_id.split(":", 1)[1] if ":" in conversation_id else conversation_id
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


async def submit_resume_turn(conversation_id, prompt, *, get_channel, fetch_channel,
                             runner_for) -> bool:
    """Queue one autonomous-resume turn into its conversation's runner.

    Free of discord types: the caller passes the cache lookup, the (async) fetch,
    and the runner factory. Returns True when the turn was queued. Going through
    the runner means the resume is serialized with any live turns on the
    conversation, so it can never race the ``claude`` session (the failure mode
    sessions.py warns about). A channel that cannot be resolved is a no-op.
    """
    channel_id = parse_conversation_channel(conversation_id)
    if channel_id is None:
        return False
    channel = get_channel(channel_id)
    if channel is None and fetch_channel is not None:
        channel = await fetch_channel(channel_id)
    if channel is None:
        return False
    runner = runner_for(conversation_id, channel)
    runner.submit(Turn(text=prompt))
    return True


def _resume_parked(config) -> bool:
    """True when the credit guard says the month is nearly spent. Fail-open:
    a broken ledger must not silently stall an enabled resume loop forever."""
    try:
        from .usage import CreditGuard

        return CreditGuard.from_config(config).should_park()
    except Exception:
        return False


def thread_name_for(text: str, limit: int = 90) -> str:
    """A Discord thread name from a task's opening message (<= 100-char limit)."""
    name = " ".join((text or "").split())  # collapse whitespace/newlines
    return name[:limit].rstrip() or "New task"


def should_auto_thread(channel, config) -> bool:
    """Whether to spin a fresh thread for a task started in this channel.

    Only in a regular guild channel: never in a DM (no threads there) and never
    when the message is already inside a thread (it just continues there).
    """
    if not config.auto_thread:
        return False
    if getattr(channel, "guild", None) is None:
        return False
    if getattr(channel, "parent_id", None) is not None:
        return False
    return True


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
    # Guards the autonomous-resume loop to a single start (on_ready re-fires on
    # every reconnect). A list so the on_ready closure can flip it.
    resume_started: list[bool] = []

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

    async def _resume_loop():
        # The consumer side of autonomous resume: drain the cross-process queue
        # the finished background command wrote, gate on credit-park + daily cap,
        # and submit each accepted request as a turn on the home conversation.
        # Bounded entirely by config; never starts a conversation from nothing.
        from .autoresume import ResumeBudget, ResumeQueue, dispatch_resumes

        queue = ResumeQueue(config.resume_queue_file)
        budget = ResumeBudget(config.resume_state_file, config.auto_resume_max_per_day)
        while not client.is_closed():
            await asyncio.sleep(config.resume_poll_secs)
            accepted: list[tuple[str, str]] = []
            try:
                dispatch_resumes(
                    queue, budget,
                    now=time.time(),
                    parked=_resume_parked(config),
                    submit=lambda conv, prompt: accepted.append((conv, prompt)),
                )
            except Exception:
                log.warning("resume dispatch failed", exc_info=True)
                continue
            for conv, prompt in accepted:
                try:
                    await submit_resume_turn(
                        conv, prompt,
                        get_channel=client.get_channel,
                        fetch_channel=client.fetch_channel,
                        runner_for=_runner_for,
                    )
                except Exception:
                    log.warning("could not submit resume for %s", conv, exc_info=True)

    @client.event
    async def on_ready():
        log.info("Connected to Discord as %s", client.user)
        # Start the resume loop once (on_ready fires again on every reconnect).
        if config.auto_resume and not resume_started:
            resume_started.append(True)
            client.loop.create_task(_resume_loop())

    @client.event
    async def on_message(message):
        if not should_handle(message, client.user, config):
            return

        conversation_id = f"discord:{message.channel.id}"
        prompt = _clean_content(message)

        # Bang commands (!usage, !jobs, !stop, ...) are a zero-inference control
        # plane: handled here, before the brain ever runs, and never submitted
        # to a turn. parse() returns None for ordinary messages and for unknown
        # !words, so real messages fall straight through.
        cmd = commands.parse(prompt)
        if cmd is not None:
            def _reset() -> None:
                agent.reset(conversation_id)
                runners.pop(conversation_id, None)  # drop queued-but-unsent turns

            def _stop() -> str:
                runner = runners.pop(conversation_id, None)
                if runner is not None and runner.cancel():
                    return ("Okay - dropped the queued messages and I won't send the "
                            "reply I'm working on. (A chat reply finishes in the "
                            "background; to stop a running job, use !stop <id>.)")
                return "Nothing is running here right now."

            def _status_fields() -> dict:
                runner = runners.get(conversation_id)
                return {
                    "busy": bool(runner and runner.busy),
                    "pending": runner.pending if runner else 0,
                    "session_turns": agent.store.turns(conversation_id),
                }

            try:
                reply = commands.dispatch(cmd, config, reset=_reset, stop=_stop,
                                          status_fields=_status_fields)
            except Exception:
                log.warning("command %s failed", cmd.name, exc_info=True)
                reply = f"Couldn't run !{cmd.name} just now."
            for piece in chunk_text(reply, DISCORD_LIMIT):
                await message.channel.send(piece)
            return

        task_title = prompt  # the user's words, for naming a thread (before describe)
        attach_paths = await _save_attachments(message.attachments, config.attachments_dir, conversation_id)
        transcripts = await asyncio.to_thread(transcribe_audio, attach_paths, transcriber)
        prompt = describe(prompt, attach_paths, transcripts)
        if not prompt:
            return

        # A new task started in the general channel gets its own thread, so the
        # channel stays a clean launcher and the work happens in a focused space.
        channel = message.channel
        if should_auto_thread(channel, config):
            try:
                thread = await message.create_thread(name=thread_name_for(task_title))
                channel = thread
                conversation_id = f"discord:{thread.id}"
            except Exception:
                log.warning("could not start a thread for the task; replying in the channel", exc_info=True)

        async def receipt() -> None:
            # Confirm a mid-task message was seen; it folds into the next turn.
            try:
                await message.add_reaction("\N{EYES}")
            except Exception:
                log.debug("could not react to mid-task message", exc_info=True)

        runner = _runner_for(conversation_id, channel)
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
