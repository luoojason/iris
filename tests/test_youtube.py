"""Tests for the channel-views core (iris/youtube.py): yt-dlp parsing + table,
with the yt-dlp runner injected so no real network or binary is touched."""

from __future__ import annotations

from iris.youtube import fetch_channel_views, format_views_table


def test_fetch_parses_view_rows():
    fake = "1200\tKai Cenat\tabc\n3\tSodapoppin\tdef\nNA\tWeird One\tghi"
    rows = fetch_channel_views("CH", runner=lambda args, **k: fake)
    assert {"views": 1200, "title": "Kai Cenat", "id": "abc"} in rows
    assert any(r["views"] is None for r in rows)  # NA -> None, not a crash
    assert len(rows) == 3


def test_fetch_builds_the_expected_yt_dlp_command():
    captured = {}

    def runner(args, **kw):
        captured["args"] = args
        return ""

    fetch_channel_views("CH123", "/usr/bin/yt-dlp", tab="shorts", limit=10, runner=runner)
    a = captured["args"]
    assert a[0] == "/usr/bin/yt-dlp" and "--flat-playlist" in a
    assert "https://www.youtube.com/channel/CH123/shorts" in a
    assert "10" in a  # the playlist-end limit


def test_format_ranks_by_views_desc_and_totals():
    rows = [{"views": 3, "title": "low", "id": "a"},
            {"views": 1200, "title": "high", "id": "b"},
            {"views": None, "title": "unknown", "id": "c"}]
    out = format_views_table(rows)
    assert out.index("high") < out.index("low") < out.index("unknown")
    assert "1,203 total" in out  # 3 + 1200, NA excluded


def test_format_empty_is_friendly():
    assert "No videos" in format_views_table([])
