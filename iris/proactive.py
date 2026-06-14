"""The proactive leash: gate self-initiated work on the real weekly plan usage.

Iris's proactive reviews (assist twice a day, maintain every 3 days) must only
run while there's headroom on the shared Max weekly limit, so they never crowd
out Jason's own interactive work. The authoritative number is the account's
seven-day utilization percentage, read from the OAuth usage endpoint and cached:
the endpoint 429s under tight polling, so the gate reads a cache refreshed at
most every ~15 minutes. When the value is unknown or the credit guard is parked,
the gate fails safe to "do not run". See
docs/superpowers/specs/2026-06-14-proactive-design.md.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Callable, Optional

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
DEFAULT_THRESHOLD = 80.0
CACHE_MAX_AGE = 900.0  # 15 min; the endpoint rate-limits tight polling


def read_oauth_token(creds_path: str | Path) -> Optional[str]:
    """The Claude.ai OAuth access token from a credentials file, or None.

    Re-read every cycle by the caller: the token rotates and expires often.
    """
    try:
        data = json.loads(Path(creds_path).read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return (data.get("claudeAiOauth") or {}).get("accessToken") or None


def fetch_weekly_utilization(token: Optional[str], opener: Optional[Callable] = None) -> Optional[float]:
    """GET the account usage; return seven_day.utilization (0-100), or None on any failure."""
    if not token:
        return None
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "iris-proactive (https://github.com/luoojason/iris, 0.1)",
    })
    try:
        opener = opener or urllib.request.urlopen
        with opener(req, timeout=20) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None
    util = (data.get("seven_day") or {}).get("utilization")
    try:
        return float(util) if util is not None else None
    except (TypeError, ValueError):
        return None


class UsageCache:
    """Last-known weekly utilization, so the gate never polls the 429-prone
    endpoint tightly. ``get`` refetches only when the cache is stale and keeps
    the last value if a refetch fails (graceful degrade)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, utilization: Optional[float], now: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"utilization": utilization, "ts": now}), "utf-8")

    def get(self, now: float, fetcher: Callable[[], Optional[float]],
            max_age: float = CACHE_MAX_AGE) -> Optional[float]:
        state = self._load()
        ts, val = state.get("ts"), state.get("utilization")
        if ts is not None and (now - ts) < max_age:
            return val  # fresh enough; do not hit the endpoint
        fresh = fetcher()
        if fresh is not None:
            self._save(fresh, now)
            return fresh
        return val  # refetch failed: fall back to last known (may be None)


def proactive_allowed(utilization: Optional[float], parked: bool,
                      threshold: float = DEFAULT_THRESHOLD) -> bool:
    """Run a proactive review only with known headroom and an unparked guard.

    Unknown usage or a parked credit guard both mean "do not run": the leash
    fails safe so a missing number never licenses unbounded self-initiated spend.
    """
    if parked or utilization is None:
        return False
    return utilization < threshold
