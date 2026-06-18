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
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("iris.proactive")

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
DEFAULT_THRESHOLD = 80.0
CACHE_MAX_AGE = 900.0  # 15 min; the endpoint rate-limits tight polling


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects on the usage fetch. This is the one place Iris's own code
    handles the raw subscription OAuth token (in an Authorization header); a 30x
    to another host would forward that token, so a redirect is an error here, not
    something to silently follow."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code, f"refusing redirect to {newurl}", headers, fp)


# Built once: an opener with the stock redirect-following handler replaced by the
# refusing one above. Tests inject their own opener; production uses this.
_no_redirect_opener = urllib.request.build_opener(_NoRedirectHandler)

# Reply this (and nothing else) when a review finds nothing worth surfacing, so
# the tick stays silent and spends no Discord noise on busywork.
SILENT = "NOTHING"

PROMPTS = {
    "assist": (
        "This is a proactive review. Jason did not ask for anything; the clock "
        "triggered you. Look at what is going on (recent threads, your memory, "
        "the wiki, scheduled jobs, open promises) and find the SINGLE highest-value "
        "thing you could do right now that Jason has not asked for but would clearly "
        "benefit him.\n"
        "- If it is small and reversible (organizing, drafting, research, prepping), "
        "do it now with your tools and briefly report what you did.\n"
        "- If it is big or outward-facing (posts publicly, spends money, messages "
        "other people, deletes things), do NOT do it: describe it in one or two "
        "lines and ask.\n"
        "- If it needs more time, schedule a job for it.\n"
        f"- If nothing genuinely clears the bar, reply with exactly {SILENT} and take "
        "no action. Do not invent busywork. Keep any report to a few lines."
    ),
    "maintain": (
        "This is a maintenance and self-improvement review (clock-triggered, not "
        "requested). Scan your state: the wiki (index, log, pages), your memory "
        "notes, your skills and standing orders, and recent outcomes (what worked, "
        "what did not).\n"
        "- Do reversible housekeeping yourself: fix the wiki index and log, "
        "consolidate obvious duplicates, write durable lessons to memory, correct "
        "stale notes.\n"
        "- For anything destructive (deleting wiki pages or memories), do NOT do it "
        "silently: list the proposed changes and ask for approval.\n"
        "- If you learned something durable about how you should work, turn it into a "
        "skill with the propose_skill tool. That only STAGES it; it never changes your "
        "behavior until Jason approves it with `iris skills approve`. Say what you "
        "proposed and why.\n"
        "- Report briefly: what you cleaned up, and what you propose. Be "
        "conservative; never delete or rewrite your own behavior without asking.\n"
        f"- If there is genuinely nothing to do, reply with exactly {SILENT}."
    ),
}


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
        opener = opener or _no_redirect_opener.open
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
            try:
                self._save(fresh, now)
            except OSError:
                # A read-only or full cache dir must not crash the cron tick that
                # consults the leash; use the fresh value and re-save next time.
                log.warning("could not write the usage cache at %s", self.path)
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


def _default_sender(channel: str, text: str, token: str) -> bool:
    from .reminders import send_discord_message
    return send_discord_message(channel, text, token)


def run_proactive_tick(config, kind: str, *, now: float,
                       agent=None, fetch: Optional[Callable] = None,
                       sender: Optional[Callable] = None) -> str:
    """One proactive review. Returns a one-word status (for the cron log).

    Gated three ways before any model call: off unless IRIS_PROACTIVE; the credit
    guard must not be parked; and the real weekly utilization must be under the
    threshold (read via a cache so the 429-prone endpoint is not polled). Only
    then does it run one model turn in a dedicated, continuous session for this
    kind and deliver a non-silent reply to the home channel. The seams (agent,
    fetch, sender) are injected by tests.
    """
    if not getattr(config, "proactive_enabled", False):
        return "disabled"
    if kind not in PROMPTS:
        return f"unknown-kind:{kind}"

    from .leash import clock_work_allowed
    allowed, reason = clock_work_allowed(config, now, fetch)
    if not allowed:
        return f"skipped({reason})"

    if agent is None:
        from .agent import Agent
        agent = Agent.from_config(config, clock_gated=True)
    result = agent.respond(f"proactive:{kind}", PROMPTS[kind])
    text = (getattr(result, "text", "") or "").strip()
    if not text or text.upper().rstrip(".!") == SILENT:
        return "silent"

    send = sender or _default_sender
    if config.home_channel and config.discord_token:
        send(config.home_channel, f"[proactive: {kind}] {text}", config.discord_token)
        return "delivered"
    return "no-channel"
