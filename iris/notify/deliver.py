"""Send a proactive message out. Discord for now; Telegram can be added later."""

from __future__ import annotations


def send(text: str, *, token: str, channel: str, sender=None) -> bool:
    """Deliver ``text`` to Discord. Returns False if unconfigured or it fails,
    so the caller can fall back to printing locally."""
    if not token or not channel:
        return False
    if sender is None:
        from ..reminders import send_discord_message
        sender = send_discord_message
    try:
        return bool(sender(channel, text, token))
    except Exception:
        return False
