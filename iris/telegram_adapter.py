"""Telegram front end. Same shape as the Discord adapter, different transport.

``python-telegram-bot`` is imported lazily so the core package and its tests do
not depend on it.
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

log = logging.getLogger("iris.telegram")

TELEGRAM_LIMIT = 4096


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
    from telegram.ext import (  # lazy
        ApplicationBuilder,
        CommandHandler,
        MessageHandler,
        filters,
    )

    app = ApplicationBuilder().token(config.telegram_token).build()
    transcriber = build_transcriber(config)  # None unless IRIS_VOICE is on

    def _allowed(update) -> bool:
        user = update.effective_user
        if not user:
            return False
        if config.allowed_user_ids and str(user.id) not in config.allowed_user_ids:
            return False
        return True

    async def reset_cmd(update, context):
        if not _allowed(update):
            return
        agent.reset(f"telegram:{update.effective_chat.id}")
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

        attach_paths = await _save_attachments(message, context, config.attachments_dir, conversation_id)
        transcripts = await asyncio.to_thread(transcribe_audio, attach_paths, transcriber)
        prompt = describe(text, attach_paths, transcripts)
        if not prompt:
            return

        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        try:
            result = await asyncio.to_thread(
                agent.respond, conversation_id, prompt, bool(attach_paths)
            )
        except ClaudeError as exc:
            log.error("claude unavailable: %s", exc)
            await message.reply_text(f"I can't reach my brain right now: {exc}")
            return

        if result.is_error:
            log.warning("turn errored: %s", result.error)
            await message.reply_text(
                "Something went wrong on that one."
                + (f" ({result.error})" if result.error else "")
            )
            return

        reply = result.text.strip() or "(no response)"
        for piece in chunk_text(reply, TELEGRAM_LIMIT):
            await message.reply_text(piece)

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
    agent = Agent.from_config(config)
    app = build_app(config, agent)
    app.run_polling()
