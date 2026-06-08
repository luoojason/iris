"""Optional per-turn telemetry, written as JSON lines when a path is configured.

Opt-in and fail-soft by design: emitting metrics must never affect a reply, and
the published agent writes nothing unless IRIS_METRICS_FILE is set. The dashboard
reads these files to show routing reasons that transcripts cannot capture.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from .driver import ClaudeResult

log = logging.getLogger("iris.metrics")


def _transport(conversation_id: str) -> str:
    """The front end is encoded as the prefix of the conversation id (e.g. 'discord:123')."""
    return conversation_id.split(":", 1)[0] if ":" in conversation_id else "unknown"


def emit_turn(
    path: str,
    conversation_id: str,
    result: ClaudeResult,
    routed: Optional[str],
    reason: str,
    has_attachments: bool,
    turns: int,
) -> None:
    """Append one metrics record for a completed turn. Never raises.

    ``routed`` is the routing outcome: "light", "strong", or "default"
    (no light model configured). ``reason`` is the router's reason string.
    """
    if not path:
        return
    try:
        record = {
            "ts": time.time(),
            "conversation_id": conversation_id,
            "transport": _transport(conversation_id),
            "session_id": result.session_id,
            "model": result.model,
            "routed": routed,
            "routing_reason": reason,
            "has_attachments": has_attachments,
            "cost_usd": result.cost_usd,
            "context_tokens": result.context_tokens,
            "duration_ms": result.duration_ms,
            "is_error": result.is_error,
            "turns": turns,
        }
        p = Path(path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except Exception as exc:  # fail-soft: telemetry must never break a turn
        log.debug("metrics emit failed: %s", exc)
