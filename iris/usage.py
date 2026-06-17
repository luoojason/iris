"""The credit guard: a usage ledger, threshold pings, and gentle brakes.

Iris draws from the plan's monthly agent credit. This module makes the draw
visible (`iris usage`, the usage MCP tool) and applies brakes long before the
credit runs dry: threshold pings from the tick, background-job parking, and
more aggressive light-model routing. Chat itself is never blocked.
See docs/superpowers/specs/2026-06-08-credit-guard-design.md.

The ledger sums the ``cost_usd`` estimates the claude CLI reports per turn.
That is a proxy for credit draw, not a bill; with no budget configured
everything still records but nothing pings, parks, or tightens.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import Config
from .statefile import JsonDictStore

log = logging.getLogger("iris.usage")


def month_key(now: Optional[float] = None) -> str:
    """UTC YYYY-MM for the ledger's month bucket."""
    now = time.time() if now is None else now
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m")


def _blank_month() -> dict:
    return {"cost_usd": 0.0, "turns": 0, "tokens": 0, "by_source": {}, "pinged": {}}


class UsageLedger:
    """Month-keyed totals, file-backed with the usual flock + atomic replace."""

    def __init__(self, path: str | os.PathLike[str]):
        self._store = JsonDictStore(path, "usage ledger", sort_keys=True)
        self.path = self._store.path

    @contextmanager
    def _locked(self):
        with self._store.locked():
            yield

    def _load(self) -> dict:
        return self._store.load()

    def _save(self, data: dict) -> None:
        self._store.save(data)

    def record(self, source: str, result, now: Optional[float] = None) -> None:
        """Add one turn's cost/tokens to the current month under ``source``."""
        cost = getattr(result, "cost_usd", None)
        tokens = getattr(result, "context_tokens", None)
        with self._locked():
            data = self._load()
            entry = data.setdefault(month_key(now), _blank_month())
            entry["turns"] = int(entry.get("turns", 0)) + 1
            if isinstance(cost, (int, float)):
                entry["cost_usd"] = round(float(entry.get("cost_usd", 0.0)) + float(cost), 6)
                by_source = entry.setdefault("by_source", {})
                by_source[source] = round(float(by_source.get(source, 0.0)) + float(cost), 6)
            if isinstance(tokens, (int, float)):
                entry["tokens"] = int(entry.get("tokens", 0)) + int(tokens)
            self._save(data)

    def month(self, now: Optional[float] = None) -> dict:
        entry = self._load().get(month_key(now)) or {}
        merged = _blank_month()
        merged.update({k: v for k, v in entry.items() if k in merged})
        return merged

    def mark_pinged(self, threshold: float, now: Optional[float] = None) -> None:
        with self._locked():
            data = self._load()
            entry = data.setdefault(month_key(now), _blank_month())
            entry.setdefault("pinged", {})[f"{threshold:g}"] = time.time() if now is None else now
            self._save(data)


def record_turn(path: str, source: str, result) -> None:
    """Record one turn, never raising: a ledger problem must not break a turn."""
    if not path:
        return
    try:
        UsageLedger(path).record(source, result)
    except Exception:
        log.warning("could not record usage for source %s", source, exc_info=True)


def month_pace(cost: float, now: Optional[float] = None) -> tuple[float, int, int]:
    """Linear month-end projection: (projected_cost, day_of_month, days_in_month).

    Elapsed time is floored at one full day so the first hours of a month do
    not project a few cents into a thousand-dollar scare.
    """
    import calendar

    now = time.time() if now is None else now
    dt = datetime.fromtimestamp(now, timezone.utc)
    days_in_month = calendar.monthrange(dt.year, dt.month)[1]
    elapsed_days = (dt.day - 1) + (dt.hour * 3600 + dt.minute * 60 + dt.second) / 86400.0
    elapsed_days = max(elapsed_days, 1.0)
    projected = cost * days_in_month / elapsed_days
    return projected, dt.day, days_in_month


def percent_used(entry: dict, budget: float) -> float:
    """Percent of the monthly budget spent; 0 when no budget is configured."""
    if not budget or budget <= 0:
        return 0.0
    return float(entry.get("cost_usd", 0.0)) / float(budget) * 100.0


def level_for(pct: float, tighten_at: float, park_at: float) -> str:
    if pct >= park_at:
        return "park"
    if pct >= tighten_at:
        return "tighten"
    return "ok"


class CreditGuard:
    """The per-process view of the ledger: levels, tightening, parking.

    The level is re-read only when the ledger file's mtime changes, so the
    router can consult it on every turn for the cost of one ``stat``.
    """

    def __init__(self, path: str, budget: float, *, tighten_at: float = 80.0,
                 park_at: float = 95.0, tighten_factor: float = 3.0):
        self.path = path
        self.budget = budget
        self.tighten_at = tighten_at
        self.park_at = park_at
        self.tighten_factor = tighten_factor
        self._cached: tuple = (None, "ok")  # (mtime, level)

    @classmethod
    def from_config(cls, config: Config) -> "CreditGuard":
        return cls(
            config.usage_file,
            config.usage_budget_usd,
            tighten_at=config.usage_tighten_at,
            park_at=config.usage_park_at,
            tighten_factor=config.tighten_factor,
        )

    def record(self, source: str, result) -> None:
        record_turn(self.path, source, result)
        self._cached = (None, "ok")  # force a re-read on the next level check

    def percent(self) -> float:
        if not self.budget or self.budget <= 0:
            return 0.0
        return percent_used(UsageLedger(self.path).month(), self.budget)

    def level(self) -> str:
        if not self.budget or self.budget <= 0:
            return "ok"
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            return "ok"
        if self._cached[0] == mtime:
            return self._cached[1]
        lvl = level_for(self.percent(), self.tighten_at, self.park_at)
        self._cached = (mtime, lvl)
        return lvl

    def should_park(self) -> bool:
        return self.level() == "park"

    def tightened_max_chars(self, base: int) -> int:
        """The trivial-routing cap, stretched when the month is running hot."""
        if self.level() in ("tighten", "park"):
            return int(base * self.tighten_factor)
        return base


def summary_text(config: Config, now: Optional[float] = None) -> str:
    """The month's draw, for `iris usage` and the usage MCP tool."""
    entry = UsageLedger(config.usage_file).month(now)
    lines = [f"month: {month_key(now)}"]
    lines.append(f"turns: {entry['turns']}, tokens: {entry['tokens']}")
    if config.usage_budget_usd > 0:
        pct = percent_used(entry, config.usage_budget_usd)
        lvl = level_for(pct, config.usage_tighten_at, config.usage_park_at)
        lines.append(
            f"spend: ${entry['cost_usd']:.2f} of ${config.usage_budget_usd:.2f} "
            f"budget ({pct:.0f}%, level: {lvl})"
        )
        if entry.get("pinged"):
            lines.append("pinged: " + ", ".join(sorted(entry["pinged"], key=float)) + "%")
    else:
        lines.append(f"spend: ${entry['cost_usd']:.2f} (no budget set; the guard is off)")
    if entry.get("cost_usd", 0) > 0:
        projected, day, days = month_pace(float(entry["cost_usd"]), now)
        pace = f"pace: ${projected:.2f} by month end (day {day} of {days})"
        if config.usage_budget_usd > 0:
            pace += " — over budget" if projected > config.usage_budget_usd else " — within budget"
        lines.append(pace)
    if entry.get("by_source"):
        parts = [f"{src} ${cost:.2f}" for src, cost in sorted(entry["by_source"].items())]
        lines.append("by source: " + ", ".join(parts))
    return "\n".join(lines)


def budget_tick(config: Config, now: Optional[float] = None, send=None) -> str:
    """Ping each newly-crossed budget threshold. Runs inside reminders-tick.

    Reads the ledger and POSTs plain text; no model call can happen here. A
    failed send is not marked, so the next tick retries it.
    """
    if not config.usage_budget_usd or config.usage_budget_usd <= 0:
        return "budget: off"
    if send is None:
        from .reminders import send_discord_message as send
    ledger = UsageLedger(config.usage_file)
    entry = ledger.month(now)
    pct = percent_used(entry, config.usage_budget_usd)
    channel = config.home_channel or config.notify_channel
    pinged = entry.get("pinged", {})
    sent = 0
    for threshold in sorted(config.usage_ping_at):
        if pct < threshold or f"{threshold:g}" in pinged:
            continue
        projected, _, _ = month_pace(float(entry.get("cost_usd", 0.0)), now)
        text = (
            f"credit guard: {pct:.0f}% of the ${config.usage_budget_usd:.2f} "
            f"monthly budget is used (crossed {threshold:g}%); "
            f"on pace for ${projected:.0f} by month end"
        )
        if channel and config.discord_token and send(channel, text, config.discord_token):
            ledger.mark_pinged(threshold, now)
            sent += 1
    suffix = f", pinged {sent}" if sent else ""
    return f"budget: {pct:.0f}% used{suffix}"
