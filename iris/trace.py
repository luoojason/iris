"""Append-only trace ledger: one structured record per ``claude -p`` invocation.

The per-turn chat metrics (``iris/metrics.py``) only see chat. This ledger sits at
the single choke point every model invocation passes through — ``ClaudeDriver.run``
— so chat, jobs, proactive reviews, the goal loop, and compaction all land one
uniform, replayable record: kind, model, outcome, an error category for the
taxonomy, timings, model turns, context tokens, and cost. That is what lets the
owner see, over time, whether Iris is getting better or worse.

Content (the prompt and the reply, and the raw error string, which can echo
content) is captured only when explicitly enabled, for privacy. Opt-in via
``IRIS_TRACE_FILE`` and fail-soft: tracing must never affect a result.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from .driver import ClaudeResult

log = logging.getLogger("iris.trace")


def classify_error(result: ClaudeResult) -> Optional[str]:
    """A coarse error category for the taxonomy, or None when the turn succeeded.

    Categories are deliberately few and stable so weekly counts stay comparable:
    dead_session, context_overflow, timeout, rate_limit, usage_limit, unknown
    (errored but no message), and other (errored with an unrecognized message).
    """
    if not result.is_error:
        return None
    blob = (result.error or "").lower()
    if not blob:
        return "unknown"
    if "no conversation found" in blob or ("session" in blob and "not found" in blob):
        return "dead_session"
    if any(s in blob for s in ("context length", "context_length", "too long",
                               "maximum context", "exceeds the maximum")):
        return "context_overflow"
    if "timed out" in blob or "timeout" in blob:
        return "timeout"
    if "rate limit" in blob or "429" in blob or "overloaded" in blob:
        return "rate_limit"
    if "usage limit" in blob or "quota" in blob or "credit" in blob:
        return "usage_limit"
    return "other"


def record_trace(
    path: str,
    kind: str,
    result: ClaudeResult,
    *,
    prompt: Optional[str] = None,
    session_id: Optional[str] = None,
    capture_content: bool = False,
) -> None:
    """Append one trace record for a completed invocation. Never raises.

    ``kind`` labels the caller (chat, job, proactive, goal, compaction, ...).
    With ``capture_content`` the prompt, reply text, and raw error are stored too;
    otherwise only metadata and the error category are kept.
    """
    if not path:
        return
    try:
        record = {
            "ts": time.time(),
            "kind": kind,
            "model": result.model,
            "session_id": result.session_id or session_id,
            "is_error": result.is_error,
            "error_category": classify_error(result),
            "cost_usd": result.cost_usd,
            "context_tokens": result.context_tokens,
            "num_turns": result.num_turns,
            "duration_ms": result.duration_ms,
        }
        if capture_content:
            record["prompt"] = prompt
            record["result_text"] = result.text
            record["error"] = result.error
        p = Path(path)
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # fail-soft: telemetry must never break a turn
        log.debug("trace emit failed: %s", exc)
