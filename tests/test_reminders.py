"""Tests for reminder time parsing, the store's recurrence handling, and the
outbound file upload helper (multipart body shape, failure paths)."""

from __future__ import annotations

import urllib.error

import pytest

from iris.reminders import ReminderStore, parse_every, parse_when, send_discord_file


def test_parse_when_relative_and_iso():
    assert parse_when("+30m", now=0) == 1800
    assert parse_when("+2h", now=0) == 7200
    assert parse_when("+1d", now=0) == 86400
    assert parse_when("2026-06-07T00:00:00Z") > 0


def test_parse_every_forms():
    assert parse_every("every 30m") == 1800
    assert parse_every("2h") == 7200  # 'every' is optional sugar
    assert parse_every("1d") == 86400
    assert parse_every("") == 0  # empty means one-shot


def test_parse_every_rejects_garbage():
    with pytest.raises(ValueError):
        parse_every("sometimes")
    with pytest.raises(ValueError):
        parse_every("every 5x")


def test_one_shot_pops_once_and_is_gone(tmp_path):
    store = ReminderStore(tmp_path / "r.json")
    store.add(due_ts=100, text="ping", channel_id="c1")
    assert [j["text"] for j in store.pop_due(now=200)] == ["ping"]
    assert store.pop_due(now=300) == []  # not rescheduled
    assert store.all() == []


def test_recurring_reschedules_from_now(tmp_path):
    store = ReminderStore(tmp_path / "r.json")
    store.add(due_ts=100, text="standup", channel_id="c1", repeat_secs=3600)
    fired = store.pop_due(now=150)
    assert [j["text"] for j in fired] == ["standup"]
    # still scheduled, next fire one period from *now*, not from the old due_ts
    remaining = store.all()
    assert len(remaining) == 1
    assert remaining[0]["due_ts"] == 150 + 3600


def test_missed_window_fires_once_not_every_occurrence(tmp_path):
    # Host asleep for a long time: a daily job that was due ages ago should fire
    # exactly once on the next tick, then resume cadence, not replay every day.
    store = ReminderStore(tmp_path / "r.json")
    store.add(due_ts=0, text="daily", channel_id="c1", repeat_secs=86400)
    fired = store.pop_due(now=10 * 86400)  # ten days late
    assert len(fired) == 1
    remaining = store.all()
    assert len(remaining) == 1
    assert remaining[0]["due_ts"] == 10 * 86400 + 86400


def test_recurring_preserves_id_and_payload(tmp_path):
    store = ReminderStore(tmp_path / "r.json")
    rid = store.add(due_ts=100, text="water", channel_id="c9", repeat_secs=60)
    store.pop_due(now=100)
    nxt = store.all()[0]
    assert nxt["id"] == rid
    assert nxt["channel_id"] == "c9"
    assert nxt["repeat_secs"] == 60


# -- send_discord_file -------------------------------------------------------
#
# The boundary is random in production (no injectable knob): the tests stay
# deterministic STRUCTURALLY, by reading the boundary back out of the
# Content-Type header and reconstructing the exact body it implies.


def make_poster(calls, status=200):
    def poster(url, body, headers):
        calls.append({"url": url, "body": body, "headers": headers})
        return status
    return poster


def test_send_discord_file_builds_a_correct_multipart_post(tmp_path):
    f = tmp_path / "report.csv"
    f.write_bytes(b"a,b\n1,2\n")
    calls = []
    assert send_discord_file("123", str(f), "tok", poster=make_poster(calls)) is True

    call = calls[0]
    assert call["url"] == "https://discord.com/api/v10/channels/123/messages"
    assert call["headers"]["Authorization"] == "Bot tok"
    ctype = call["headers"]["Content-Type"]
    assert ctype.startswith("multipart/form-data; boundary=")

    boundary = ctype.split("boundary=", 1)[1]
    expected = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="files[0]"; filename="report.csv"\r\n'
        "Content-Type: application/octet-stream\r\n"
        "\r\n"
    ).encode("utf-8") + b"a,b\n1,2\n" + f"\r\n--{boundary}--\r\n".encode("utf-8")
    assert call["body"] == expected


def test_send_discord_file_carries_binary_bytes_verbatim(tmp_path):
    payload = b"\x89PNG\r\n\x1a\n\x00\x01\x02"
    f = tmp_path / "shot.png"
    f.write_bytes(payload)
    calls = []
    assert send_discord_file("9", str(f), "tok", poster=make_poster(calls)) is True
    assert payload in calls[0]["body"]
    assert b'filename="shot.png"' in calls[0]["body"]


def test_send_discord_file_missing_file_is_false_without_posting(tmp_path):
    def poster(url, body, headers):
        raise AssertionError("nothing should be posted for a missing file")
    assert send_discord_file("123", str(tmp_path / "gone.txt"), "tok", poster=poster) is False


def test_send_discord_file_directory_is_false(tmp_path):
    assert send_discord_file("123", str(tmp_path), "tok", poster=make_poster([])) is False


def test_send_discord_file_non_2xx_is_false(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert send_discord_file("123", str(f), "tok", poster=make_poster([], status=413)) is False


def test_send_discord_file_other_2xx_is_true(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert send_discord_file("123", str(f), "tok", poster=make_poster([], status=204)) is True


@pytest.mark.parametrize("exc", [
    OSError("connection reset"),
    urllib.error.HTTPError("u", 429, "rate limited", None, None),
])
def test_send_discord_file_poster_failures_return_false(tmp_path, exc):
    f = tmp_path / "a.txt"
    f.write_text("x")

    def poster(url, body, headers):
        raise exc

    assert send_discord_file("123", str(f), "tok", poster=poster) is False  # never raises
