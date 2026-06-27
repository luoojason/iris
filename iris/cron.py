"""A small, dependency-free 5-field cron evaluator anchored to a timezone.

Iris's reminders and schedules only understood ``+30m`` / ``every 2h`` intervals
in UTC, so there was no way to say "every weekday at 09:00 my time" (the thing a
personal agent is most often asked for). This adds standard 5-field cron
(``minute hour day-of-month month day-of-week``) evaluated in an IANA timezone,
as a pure ``next_fire`` the reminder/schedule code calls to compute the next due
timestamp. Stdlib only (``zoneinfo``), so the package keeps its zero-core-dep
posture.

Day-of-week is cron-style 0=Sunday..6=Saturday (7 also Sunday). When BOTH
day-of-month and day-of-week are restricted, a day matches if EITHER does, which
is POSIX cron semantics.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - zoneinfo is stdlib on 3.9+
    ZoneInfo = None  # type: ignore

_FULL_DOM = frozenset(range(1, 32))
_FULL_DOW = frozenset(range(0, 7))


def _parse_field(field: str, lo: int, hi: int) -> set:
    """Expand one cron field (``*``, ``a``, ``a-b``, ``a-b/n``, ``*/n``, lists) to a set."""
    allowed: set = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError("empty cron field element")
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            step = int(step_s)
            if step <= 0:
                raise ValueError("cron step must be positive")
        if part in ("*", ""):
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        if start > end:
            raise ValueError("cron range start after end")
        for value in range(start, end + 1, step):
            if not (lo <= value <= hi):
                raise ValueError(f"cron value {value} out of range [{lo},{hi}]")
            allowed.add(value)
    return allowed


def parse_cron(spec: str) -> Optional[dict]:
    """Parse a 5-field cron string into field sets, or None if it is not valid cron."""
    parts = (spec or "").split()
    if len(parts) != 5:
        return None
    try:
        minute = _parse_field(parts[0], 0, 59)
        hour = _parse_field(parts[1], 0, 23)
        dom = _parse_field(parts[2], 1, 31)
        month = _parse_field(parts[3], 1, 12)
        dow_raw = _parse_field(parts[4], 0, 7)
    except ValueError:
        return None
    dow = {0 if d == 7 else d for d in dow_raw}  # 7 == Sunday == 0
    if not all((minute, hour, dom, month, dow)):
        return None
    return {"minute": minute, "hour": hour, "dom": dom, "month": month, "dow": dow}


def _matches(local: "_dt.datetime", fields: dict) -> bool:
    if local.minute not in fields["minute"]:
        return False
    if local.hour not in fields["hour"]:
        return False
    if local.month not in fields["month"]:
        return False
    cron_dow = (local.weekday() + 1) % 7  # python Mon=0..Sun=6 -> cron Sun=0..Sat=6
    dom_restricted = fields["dom"] != _FULL_DOM
    dow_restricted = fields["dow"] != _FULL_DOW
    dom_ok = local.day in fields["dom"]
    dow_ok = cron_dow in fields["dow"]
    if dom_restricted and dow_restricted:
        return dom_ok or dow_ok  # POSIX cron OR semantics
    if dom_restricted:
        return dom_ok
    if dow_restricted:
        return dow_ok
    return True


def _zone(tz: str):
    if tz and tz != "UTC" and ZoneInfo is not None:
        try:
            return ZoneInfo(tz)
        except Exception:
            return _dt.timezone.utc
    return _dt.timezone.utc


def next_fire(spec: str, after_ts: float, tz: str = "UTC") -> Optional[float]:
    """The next epoch timestamp strictly after ``after_ts`` matching ``spec`` in ``tz``.

    Returns None if ``spec`` is not a valid 5-field cron or nothing matches within
    a year (e.g. an impossible date). Steps absolute epoch minutes and converts to
    local time for the field check, so DST transitions are handled correctly (a
    skipped hour is simply never matched; a repeated hour matches twice).
    """
    fields = parse_cron(spec)
    if fields is None:
        return None
    tzinfo = _zone(tz)
    ts = (int(after_ts) // 60 + 1) * 60  # the next whole minute
    for _ in range(366 * 24 * 60):  # at most ~one year of minutes
        local = _dt.datetime.fromtimestamp(ts, tz=tzinfo)
        if _matches(local, fields):
            return float(ts)
        ts += 60
    return None
