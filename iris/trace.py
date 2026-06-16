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
from collections import Counter
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


def load_traces(path: str, since_ts: Optional[float] = None) -> list[dict]:
    """Read trace records from a JSONL ledger, newest-last. Bad lines are skipped.

    With ``since_ts`` only records at or after that timestamp are returned.
    A missing or unreadable file reads as no records.
    """
    p = Path(path)
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                if since_ts is not None and (rec.get("ts") or 0) < since_ts:
                    continue
                out.append(rec)
    except OSError:
        return []
    return out


def _nums(records: list[dict], key: str) -> list[float]:
    return [r[key] for r in records if isinstance(r.get(key), (int, float))]


def summarize_traces(records: list[dict]) -> dict:
    """Aggregate trace records into counts, cost, latency, and the error taxonomy.

    Pure: it computes the numbers a weekly digest needs without touching disk or
    a model, so the digest can be rendered and delivered through the notify spine.
    """
    total = len(records)
    errors = [r for r in records if r.get("is_error")]
    costs = _nums(records, "cost_usd")
    durations = _nums([r for r in records if not r.get("is_error")], "duration_ms")
    turns = _nums(records, "num_turns")
    by_error = Counter(r.get("error_category") for r in errors if r.get("error_category"))
    return {
        "runs": total,
        "errors": len(errors),
        "error_rate": (len(errors) / total) if total else 0.0,
        "by_kind": dict(Counter(r.get("kind") for r in records)),
        "by_error_category": dict(by_error),
        "total_cost_usd": round(sum(costs), 4),
        "avg_duration_ms": (sum(durations) / len(durations)) if durations else None,
        "total_turns": int(sum(turns)),
    }


def render_digest(summary: dict, days: Optional[int] = None) -> str:
    """A compact, model-free text digest of a trace summary, for the notify spine."""
    window = f" (last {days}d)" if days else ""
    runs = summary.get("runs", 0)
    errors = summary.get("errors", 0)
    rate = summary.get("error_rate", 0.0) * 100
    lines = [f"Iris trace digest{window}: {runs} runs, {errors} errors ({rate:.0f}%)."]
    by_kind = summary.get("by_kind") or {}
    if by_kind:
        lines.append("by kind: " + ", ".join(f"{k} {n}" for k, n in sorted(by_kind.items())))
    by_error = summary.get("by_error_category") or {}
    if by_error:
        lines.append("errors: " + ", ".join(f"{k} {n}" for k, n in sorted(by_error.items())))
    cost = summary.get("total_cost_usd") or 0.0
    avg_ms = summary.get("avg_duration_ms")
    tail = [f"cost ${cost:.2f}", f"{summary.get('total_turns', 0)} model turns"]
    if avg_ms:
        tail.append(f"avg {avg_ms / 1000:.1f}s/run")
    lines.append("; ".join(tail) + ".")
    return "\n".join(lines)
