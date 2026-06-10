"""Pure budget arithmetic over the metrics JSONL.

Everything here is file reads and templated strings: no model calls, no
network. The usage CLI, the usage MCP tool, and the reminders tick all read
spend through these functions; timestamps are always passed in, never sampled.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

THRESHOLDS = (50, 80, 95)

# Floor on the elapsed fraction of the month so a few cents spent minutes
# after rollover cannot project to an absurd month-end figure.
_MIN_MONTH_FRACTION = 0.01


def read_metrics(path: str | os.PathLike[str], since_ts: float = 0.0) -> list[dict]:
    """Read metrics records with ts >= since_ts; bad lines are skipped."""
    if not path:
        return []
    p = Path(path)
    try:
        text = p.read_text("utf-8")
    except OSError:
        return []
    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        ts = rec.get("ts", 0.0)
        if isinstance(ts, (int, float)) and ts >= since_ts:
            records.append(rec)
    return records


def _cost(rec: dict) -> float:
    cost = rec.get("cost_usd")
    return float(cost) if isinstance(cost, (int, float)) else 0.0


def _transport(conversation_id: str) -> str:
    """Prefix before ':' encodes the front end (metrics.py convention)."""
    return conversation_id.split(":", 1)[0] if ":" in conversation_id else "unknown"


def _p95(values: list) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]  # nearest rank


def summarize(records: list[dict]) -> dict:
    """Totals and breakdowns for a batch of metrics records."""
    by_model: dict[str, float] = {}
    by_transport: dict[str, float] = {}
    by_conversation: dict[str, float] = {}
    total = 0.0
    errors = 0
    tokens: list = []
    for rec in records:
        cost = _cost(rec)
        total += cost
        model = rec.get("model") or "unknown"
        by_model[model] = by_model.get(model, 0.0) + cost
        cid = rec.get("conversation_id") or ""
        transport = _transport(cid)
        by_transport[transport] = by_transport.get(transport, 0.0) + cost
        key = cid or "unknown"
        by_conversation[key] = by_conversation.get(key, 0.0) + cost
        if rec.get("is_error"):
            errors += 1
        ctx = rec.get("context_tokens")
        if isinstance(ctx, (int, float)):
            tokens.append(ctx)
    turns = len(records)
    top = sorted(by_conversation.items(), key=lambda kv: kv[1], reverse=True)[:5]
    return {
        "total_cost": total,
        "turns": turns,
        "errors": errors,
        "error_rate": errors / turns if turns else 0.0,
        "by_model": by_model,
        "by_transport": by_transport,
        "top_conversations": top,
        "context_p95": _p95(tokens),
    }


def window(now: float, period: str) -> float:
    """Start-of-period timestamp (calendar boundaries, local time)."""
    day = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "day":
        return day.timestamp()
    if period == "week":
        return (day - timedelta(days=day.weekday())).timestamp()
    if period == "month":
        return day.replace(day=1).timestamp()
    raise ValueError(f"unknown period: {period!r}")


def _month_bounds(now: float) -> tuple[float, float]:
    start = datetime.fromtimestamp(now).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.timestamp(), end.timestamp()


def projection(month_records: list[dict], now: float) -> float:
    """Linear month-end spend estimate from this month's records."""
    spent = sum(_cost(rec) for rec in month_records)
    start, end = _month_bounds(now)
    elapsed = (now - start) / (end - start)
    return spent / max(elapsed, _MIN_MONTH_FRACTION)


def thresholds_crossed(spent: float, credit: float, already_pinged: set[int]) -> list[int]:
    """Which of the 50/80/95 percent marks are newly crossed, ascending."""
    if credit <= 0:
        return []
    pct = spent / credit * 100
    return [t for t in THRESHOLDS if pct >= t and t not in already_pinged]


def month_key(now: float) -> str:
    """The local-time month bucket for a timestamp, e.g. '2026-06'."""
    return datetime.fromtimestamp(now).strftime("%Y-%m")


def format_summary(summary: dict, *, credit: float = 0.0, projection: Optional[float] = None) -> str:
    """Render a summary as text; shared by the CLI and the usage MCP tool."""
    lines = [f"spend: ${summary['total_cost']:.2f} ({summary['turns']} turns)"]

    def breakdown(label: str, buckets: dict) -> None:
        if not buckets:
            return
        lines.append(f"{label}:")
        for name, cost in sorted(buckets.items(), key=lambda kv: kv[1], reverse=True):
            lines.append(f"  {name}: ${cost:.2f}")

    breakdown("by model", summary["by_model"])
    breakdown("by transport", summary["by_transport"])
    lines.append(
        f"errors: {summary['errors']}/{summary['turns']}"
        f" ({summary['error_rate'] * 100:.1f}%)"
    )
    lines.append(f"context p95: {summary['context_p95']} tokens")
    if summary["top_conversations"]:
        lines.append("top conversations:")
        for cid, cost in summary["top_conversations"]:
            lines.append(f"  {cid}: ${cost:.2f}")
    if credit > 0:
        pct = summary["total_cost"] / credit * 100
        lines.append(f"credit: ${summary['total_cost']:.2f} of ${credit:.2f} ({pct:.1f}% used)")
        if projection is not None:
            lines.append(f"projected month end: ${projection:.2f}")
    return "\n".join(lines)


class BudgetState:
    """Tiny JSON state for threshold pings and job parking.

    Keys: ``month`` ('2026-06'), ``pinged`` (threshold percents already sent
    this month), ``park_until`` (epoch). Writes are atomic like
    SessionStore._flush; a corrupt file loads as a clean slate.
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._data: dict = {"month": "", "pinged": [], "park_until": 0.0}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return
        if not isinstance(raw, dict):
            return
        if isinstance(raw.get("month"), str):
            self._data["month"] = raw["month"]
        if isinstance(raw.get("pinged"), list):
            self._data["pinged"] = [
                int(t) for t in raw["pinged"] if isinstance(t, (int, float))
            ]
        if isinstance(raw.get("park_until"), (int, float)):
            self._data["park_until"] = float(raw["park_until"])

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def pinged(self, month: str) -> set[int]:
        """Thresholds already pinged this month; a new month reads empty."""
        return set(self._data["pinged"]) if self._data["month"] == month else set()

    def record_pings(self, month: str, thresholds: Iterable[int]) -> None:
        """Mark thresholds as pinged; a month change resets the slate first."""
        if self._data["month"] != month:
            self._data["month"] = month
            self._data["pinged"] = []
        self._data["pinged"] = sorted(set(self._data["pinged"]) | {int(t) for t in thresholds})
        self._flush()

    @property
    def park_until(self) -> float:
        return self._data["park_until"]

    def set_park_until(self, ts: float) -> None:
        self._data["park_until"] = float(ts)
        self._flush()
