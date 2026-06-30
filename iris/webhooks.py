"""Webhook wakes: a small inbound HTTP listener that turns an authorized POST
into a wake (a Discord ping plus a fold-back inbox note).

Where event wakes *poll* (the reminders tick stats files and fetches URLs),
this lets an external system *push*: a CI run, a home-automation rule, a cron on
another box can POST here and have Iris surface it. Like every wake, it **never
calls the model** — the payload only ever becomes text in a note for the owner's
next turn; it is never executed, parsed as config, or fed to the model.

Security model (this is an inbound network surface, so it is deliberately tight):

* **Off by default** (``IRIS_WEBHOOK``) and bound to ``127.0.0.1`` unless the
  owner widens it (e.g. to a tailnet address). It is never 0.0.0.0 by default.
* **A shared token is mandatory.** The server refuses to start without
  ``IRIS_WEBHOOK_TOKEN``, and every request is checked with a constant-time
  compare; an unauthenticated listener is never exposed.
* **Bounded input.** The body is capped, and the payload becomes a single
  truncated note — no unbounded reads, no code paths off the text.

The server runs as its own process (``iris webhook``), like the bots and the
reminders tick, and shares the inbox file + bot token through config. The pure
core (``handle_hook``, ``build_message``, ``_authorized``) is what carries the
logic and is unit-tested; the socket layer is a thin wrapper.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Optional
from urllib.parse import urlparse

from .config import Config
from .inbox import Inbox

log = logging.getLogger("iris.webhooks")

MAX_BODY = 64 * 1024
MAX_MESSAGE = 1000
MAX_NAME = 64


def _authorized(provided: Optional[str], expected: str) -> bool:
    """Constant-time token check. An empty configured token is never authorized."""
    if not expected:
        return False
    return hmac.compare_digest(str(provided or ""), str(expected))


def build_message(name: str, body: str) -> str:
    """Render a hook into a note. A JSON body's ``message`` field is preferred,
    else the raw body; truncated so a flood can't blow the next turn's context."""
    name = ((name or "hook").strip() or "hook")[:MAX_NAME]
    text = (body or "").strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and obj.get("message"):
            text = str(obj["message"]).strip()
    except (ValueError, TypeError):
        pass
    text = text[:MAX_MESSAGE]
    return f"webhook {name}" + (f": {text}" if text else "")


def handle_hook(config: Config, *, name: str, body: str, token: Optional[str],
                sender=None, inbox: Optional[Inbox] = None) -> tuple[int, str]:
    """The pure core: authorize, then deliver a hook as a wake. Returns (status, text).

    Gated three ways: off unless IRIS_WEBHOOK; refuses (503) without a configured
    token; rejects (401) a wrong token. An authorized hook pings the channel and
    folds a note into the inbox — never a model call. Seams (sender, inbox) are
    injected by tests.
    """
    if not getattr(config, "webhook_enabled", False):
        return 404, "webhooks are disabled"
    if not config.webhook_token:
        return 503, "no webhook token configured (set IRIS_WEBHOOK_TOKEN)"
    if not _authorized(token, config.webhook_token):
        return 401, "unauthorized"

    message = build_message(name, body)
    channel = config.webhook_channel or config.home_channel or config.notify_channel
    inbox = inbox or Inbox(config.inbox_file)
    inbox.append(message, conversation_id=(f"discord:{channel}" if channel else None))
    if sender is None:
        from .reminders import send_discord_message as sender
    if channel and config.discord_token:
        try:
            sender(channel, message, config.discord_token)
        except Exception:
            log.warning("webhook could not ping %s", channel, exc_info=True)
    return 200, "ok"


def _make_handler(config: Config):
    from http.server import BaseHTTPRequestHandler

    class _WebhookHandler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 (http.server contract)
            try:
                length = min(int(self.headers.get("Content-Length") or 0), MAX_BODY)
            except (TypeError, ValueError):
                length = 0
            body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            parsed = urlparse(self.path)
            name = parsed.path.strip("/").split("/")[-1] or "hook"
            token = (self.headers.get("X-Iris-Token")
                     or _bearer(self.headers.get("Authorization")))
            status, text = handle_hook(config, name=name, body=body, token=token)
            payload = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):  # keep the listener quiet
            return

    return _WebhookHandler


def _bearer(value: Optional[str]) -> Optional[str]:
    if value and value.lower().startswith("bearer "):
        return value[7:].strip()
    return None


def run_webhook_server(config: Config) -> int:
    """Bind and serve the webhook listener (blocking). Its own process, like the bots."""
    if not config.webhook_enabled:
        print("webhook: disabled (set IRIS_WEBHOOK=true).")
        return 1
    if not config.webhook_token:
        print("webhook: refusing to start without IRIS_WEBHOOK_TOKEN (an "
              "unauthenticated listener is unsafe).")
        return 1
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer((config.webhook_bind, config.webhook_port),
                                 _make_handler(config))
    log.info("webhook listening on %s:%s", config.webhook_bind, config.webhook_port)
    print(f"webhook listening on {config.webhook_bind}:{config.webhook_port} "
          f"(POST with X-Iris-Token). Ctrl-C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
