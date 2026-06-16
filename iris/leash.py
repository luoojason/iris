"""The clock-work leash: one answer to "is there budget headroom right now?"

Self-initiated work — proactive reviews, the goal loop, scheduled jobs — must all
ask the same question before spending: the credit guard must not be parked AND the
real weekly utilization must be under the threshold (read via a cache so the
429-prone usage endpoint is not polled tightly). Both unknown usage and a broken
ledger fail safe to "do not run", so a missing number never licenses unbounded
spend. Centralizing it here closes the coherence trap where a policy change
reached some self-starting paths and not others.
"""

from __future__ import annotations

import os
from typing import Callable, Optional


def clock_work_allowed(config, now: float,
                       fetch: Optional[Callable[[], Optional[float]]] = None) -> tuple[bool, str]:
    """Whether self-initiated work may run now. Returns ``(allowed, reason)``.

    ``fetch`` overrides the utilization fetcher (tests inject it); by default it
    reads the OAuth token and queries the usage endpoint. ``reason`` is a compact
    ``util=...,parked=...`` string for the caller's log/skip message.
    """
    from .proactive import (
        UsageCache,
        fetch_weekly_utilization,
        proactive_allowed,
        read_oauth_token,
    )

    try:
        from .usage import CreditGuard
        parked = CreditGuard.from_config(config).should_park()
    except Exception:
        # A broken ledger drops only the park backstop; the weekly-usage gate
        # below is then the sole leash (and an unknown value fails safe).
        parked = False

    creds = config.proactive_creds_path or os.path.expanduser("~/.claude/.credentials.json")
    fetcher = fetch or (lambda: fetch_weekly_utilization(read_oauth_token(creds)))
    utilization = UsageCache(config.proactive_usage_cache).get(now, fetcher)
    allowed = proactive_allowed(utilization, parked, config.proactive_usage_max)
    return allowed, f"util={utilization},parked={parked}"
