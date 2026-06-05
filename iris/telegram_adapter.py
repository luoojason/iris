"""Telegram front end. Same shape as the Discord adapter, different transport.

``python-telegram-bot`` is imported lazily so the core package and its tests do
not depend on it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .agent import Agent
from .config import Config
from .driver import ClaudeError
from .textutil import chunk_text

log = logging.getLogger("iris.telegram")

TELEGRAM_LIMIT = 4096


def build_app(config: Config, agent: Agent):
    """Build (but do not start) the Telegram application. Returns the app."""
    from telegram.ext import (  # lazy
        ApplicationBuilder,
        CommandHandler,
        MessageHandler,
        filters,
    )

    app = ApplicationBuilder().token(config.telegram_token).build()

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
        if not message or not message.text:
            return

        chat = update.effective_chat
        text = message.text.strip()

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

        if not text:
            return

        conversation_id = f"telegram:{chat.id}"
        await context.bot.send_chat_action(chat_id=chat.id, action="typing")
        try:
            result = await asyncio.to_thread(agent.respond, conversation_id, text)
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    return app


def run(config: Optional[Config] = None) -> None:
    """Load config, wire everything up, and run the Telegram bot."""
    config = config or Config.from_env()
    if not config.telegram_token:
        raise SystemExit("IRIS_TELEGRAM_TOKEN is not set. See .env.example.")
    agent = Agent.from_config(config)
    app = build_app(config, agent)
    app.run_polling()
