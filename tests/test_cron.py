"""Tests for the 5-field cron evaluator (pure, timezone-aware)."""

from __future__ import annotations

import datetime as dt

from iris.cron import next_fire, parse_cron


def _utc(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone.utc).timestamp()


def test_parse_rejects_non_cron():
    assert parse_cron("+30m") is None
    assert parse_cron("0 9 * *") is None  # 4 fields
    assert parse_cron("99 9 * * *") is None  # minute out of range


def test_parse_accepts_standard_forms():
    assert parse_cron("0 9 * * 1-5") is not None
    assert parse_cron("*/15 * * * *") is not None


def test_daily_at_nine_utc():
    # from 08:00 UTC -> next 09:00 UTC the same day
    nxt = next_fire("0 9 * * *", _utc(2026, 6, 1, 8, 0), tz="UTC")
    assert dt.datetime.fromtimestamp(nxt, tz=dt.timezone.utc) == dt.datetime(2026, 6, 1, 9, 0, tzinfo=dt.timezone.utc)


def test_weekday_only_skips_the_weekend():
    # 2026-06-06 is a Saturday; "weekdays at 09:00" from Sat must land on Mon 2026-06-08
    sat = _utc(2026, 6, 6, 12, 0)
    nxt = next_fire("0 9 * * 1-5", sat, tz="UTC")
    got = dt.datetime.fromtimestamp(nxt, tz=dt.timezone.utc)
    assert got.weekday() == 0 and got.day == 8 and got.hour == 9


def test_every_fifteen_minutes():
    nxt = next_fire("*/15 * * * *", _utc(2026, 6, 1, 9, 7), tz="UTC")
    got = dt.datetime.fromtimestamp(nxt, tz=dt.timezone.utc)
    assert got.minute == 15 and got.hour == 9


def test_timezone_anchoring():
    # "09:00 in America/New_York" is 13:00 or 14:00 UTC depending on DST. On a June
    # date (EDT, UTC-4) it is 13:00 UTC.
    base = _utc(2026, 6, 1, 0, 0)
    nxt = next_fire("0 9 * * *", base, tz="America/New_York")
    got = dt.datetime.fromtimestamp(nxt, tz=dt.timezone.utc)
    assert got.hour == 13  # 09:00 EDT == 13:00 UTC


def test_impossible_date_returns_none():
    assert next_fire("0 0 30 2 *", _utc(2026, 1, 1, 0, 0), tz="UTC") is None  # Feb 30 never
