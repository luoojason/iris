"""Telegram front end. Same shape as the Discord adapter, different transport.

``python-telegram-bot`` is imported lazily so the core package and its tests do
not depend on it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from . import commands
from .agent import Agent
from .attachments import conversation_dir, describe, safe_filename
from .config import Config
from .conversation import ConversationRunner, Turn
from .driver import ClaudeError
from .textutil import chunk_text
from .transcribe import build_transcriber, transcribe_audio

# Short interim lines, shared in spirit with the Discord adapter, so a long turn
# on Telegram is not a silent wait either.
_ACK_LINES = ("on it", "on it, one sec", "working on it", "give me a moment",
              "digging into this", "let me take a look")

log = logging.getLogger("iris.telegram")

TELEGRAM_LIMIT = 4096


def is_allowed_update(update, config: Config) -> bool:
    """Whether this Telegram update is from an allowed user. Pure, so it is testable.

    The single-user gate: with IRIS_ALLOWED_USER_IDS set, only those ids are
    answered; empty means anyone (the operator's choice, warned about at startup).
    """
    user = getattr(update, "effective_user", None)
    if not user:
        return False
    if config.allowed_user_ids and str(user.id) not in config.allowed_user_ids:
        return False
    return True


async def _save_attachments(message, context, base_dir: str, conversation_id: str) -> list[str]:
    """Download a Telegram photo/document and return its absolute path."""
    paths: list[str] = []
    if not base_dir:
        return paths
    wanted = []  # (file_id, filename)
    if getattr(message, "photo", None):
        photo = message.photo[-1]  # largest size
        wanted.append((photo.file_id, f"photo_{photo.file_unique_id}.jpg"))
    doc = getattr(message, "document", None)
    if doc:
        wanted.append((doc.file_id, doc.file_name or f"doc_{doc.file_unique_id}"))
    # Voice notes and audio clips: keep the .ogg/.oga extension so the
    # transcriber recognizes them as audio.
    voice = getattr(message, "voice", None)
    if voice:
        wanted.append((voice.file_id, f"voice_{voice.file_unique_id}.ogg"))
    audio = getattr(message, "audio", None)
    if audio:
        ext = ".oga" if not (audio.file_name and "." in audio.file_name) else ""
        wanted.append((audio.file_id, (audio.file_name or f"audio_{audio.file_unique_id}") + ext))
    if not wanted:
        return paths
    conv_dir = conversation_dir(base_dir, conversation_id)
    for file_id, filename in wanted[:5]:
        dest = conv_dir / safe_filename(filename)
        try:
            tg_file = await context.bot.get_file(file_id)
            await tg_file.download_to_drive(str(dest))
            paths.append(str(dest.resolve()))
        except Exception as exc:
            log.warning("could not save telegram attachment: %s", exc)
    return paths


def build_app(config: Config, agent: Agent):
    """Build (but do not start) the Telegram application. Returns the app."""
    try:
        from telegram.ext import (  # lazy
            ApplicationBuilder,
            CommandHandler,
            MessageHandler,
            filters,
        )
    except ImportError as exc:
        raise SystemExit(
            "Telegram support needs the extra: pip install 'iris-agent[telegram]'"
        ) from exc

    app = ApplicationBuilder().token(config.telegram_token).build()
    transcriber = build_transcriber(config)  # None unless IRIS_VOICE is on
    bot = app.bot
    # One runner per conversation, exactly like Discord: serialize a conversation's
    # turns (never two claude --resume at once), coalesce messages that pile up
    # while a turn runs, and fire a short interim ack on a slow turn.
    runners: dict[str, ConversationRunner] = {}

    def _allowed(update) -> bool:
        return is_allowed_update(update, config)

    def _preserve_undelivered(conversation_id: str):
        def preserve(text: str) -> None:
            try:
                from .statefile import JsonListStore
                store = JsonListStore(config.undelivered_file, "undelivered replies")
                with store.locked():
                    items = store.load()
                    items.append({"conversation_id": conversation_id, "text": text})
                    store.save(items[-200:])
            except Exception:
                log.warning("could not preserve an undelivered reply", exc_info=True)
        return preserve

    def _typing_for(chat_id):
        class _Typing:
            async def __aenter__(self):
                self._stop = asyncio.Event()

                async def loop():
                    while not self._stop.is_set():
                        try:
                            await bot.send_chat_action(chat_id=chat_id, action="typing")
                        except Exception:
                            pass
                        try:
                            await asyncio.wait_for(self._stop.wait(), timeout=4.0)
                        except asyncio.TimeoutError:
                            pass
                self._task = asyncio.create_task(loop())
                return self

            async def __aexit__(self, *exc):
                self._stop.set()
                await self._task
        return _Typing()

    def _runner_for(conversation_id: str, chat_id) -> ConversationRunner:
        runner = runners.get(conversation_id)
        if runner is not None:
            return runner

        async def send(text: str) -> None:
            for piece in chunk_text(text, TELEGRAM_LIMIT):
                await bot.send_message(chat_id=chat_id, text=piece)

        async def run_turn(prompt: str, has_attachments: bool) -> Optional[str]:
            try:
                result = await asyncio.to_thread(
                    agent.respond, conversation_id, prompt, has_attachments)
            except ClaudeError as exc:
                log.error("claude unavailable: %s", exc)
                return f"I can't reach my brain right now: {exc}"
            if result.is_error:
                log.warning("turn errored: %s", result.error)
                return ("Something went wrong on that one."
                        + (f" ({result.error})" if result.error else ""))
            return result.text.strip() or "(no response)"

        import random
        runner = ConversationRunner(
            run_turn=run_turn,
            send=send,
            ack_line=lambda: random.choice(_ACK_LINES),
            typing=lambda: _typing_for(chat_id),
            ack_delay=config.ack_delay,
            on_undelivered=_preserve_undelivered(conversation_id),
        )
        runners[conversation_id] = runner
        return runner

    async def reset_cmd(update, context):
        if not _allowed(update):
            return
        conversation_id = f"telegram:{update.effective_chat.id}"
        agent.reset(conversation_id)
        runners.pop(conversation_id, None)  # drop any queued-but-unsent turns
        await update.message.reply_text("Started a fresh conversation.")

    async def on_message(update, context):
        if not _allowed(update):
            return
        message = update.message
        if not message:
            return

        chat = update.effective_chat
        text = (message.text or message.caption or "").strip()
        conversation_id = f"telegram:{chat.id}"

        # In groups, only answer when addressed, unless told otherwise.
        if chat and chat.type in ("group", "supergroup") and not config.respond_without_mention:
            username = context.bot.username
            mentioned = bool(username and f"@{username}" in text)
            replied = bool(
                message.reply_to_message
                and message.reply_to_message.from_user
                and message.reply_to_message.from_user.id == context.bot.id
            )
            if not (mentioned or replied):
                return
            if username:
                text = text.replace(f"@{username}", "").strip()

        # Bang commands (!usage, !jobs, !stop, !new, ...) are the zero-inference
        # control plane, handled before the brain ever runs, same as on Discord.
        cmd = commands.parse(text)
        if cmd is not None:
            def _reset() -> None:
                agent.reset(conversation_id)
                runners.pop(conversation_id, None)

            def _stop() -> str:
                runner = runners.pop(conversation_id, None)
                if runner is not None and runner.cancel():
                    return "Okay - dropped the queued messages and the reply I was working on."
                return "Nothing is running here right now."

            def _status_fields() -> dict:
                runner = runners.get(conversation_id)
                return {"busy": bool(runner and runner.busy),
                        "pending": runner.pending if runner else 0,
                        "session_turns": agent.store.turns(conversation_id)}

            try:
                reply = commands.dispatch(cmd, config, reset=_reset, stop=_stop,
                                          status_fields=_status_fields)
            except Exception:
                log.warning("command %s failed", cmd.name, exc_info=True)
                reply = f"Couldn't run !{cmd.name} just now."
            for piece in chunk_text(reply, TELEGRAM_LIMIT):
                await message.reply_text(piece)
            return

        attach_paths = await _save_attachments(message, context, config.attachments_dir, conversation_id)
        transcripts = await asyncio.to_thread(transcribe_audio, attach_paths, transcriber)
        prompt = describe(text, attach_paths, transcripts)
        if not prompt:
            return

        async def receipt() -> None:
            try:
                await message.set_reaction("\N{EYES}")
            except Exception:
                log.debug("could not react to mid-task telegram message", exc_info=True)

        runner = _runner_for(conversation_id, chat.id)
        runner.submit(Turn(text=prompt, has_attachments=bool(attach_paths), receipt=receipt))

    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.VOICE | filters.AUDIO)
        & ~filters.COMMAND,
        on_message,
    ))
    return app


def run(config: Optional[Config] = None) -> None:
    """Load config, wire everything up, and run the Telegram bot."""
    config = config or Config.from_env()
    if not config.telegram_token:
        raise SystemExit("IRIS_TELEGRAM_TOKEN is not set. See .env.example.")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    if not config.allowed_user_ids:
        log.warning(
            "IRIS_ALLOWED_USER_IDS is empty: this bot will answer ANYONE who can "
            "reach it. A personal subscription is single-user only; set it to your id."
        )
    agent = Agent.from_config(config)
    app = build_app(config, agent)
    app.run_polling()
