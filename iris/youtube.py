"""Read a YouTube channel's public videos and view counts via yt-dlp.

No browser, no login, no model: yt-dlp scrapes the public listing in a few
seconds. This is the simple path for "how many views"; the browser is only worth
it for the logged-in Studio analytics (CTR, retention, impressions) or actions
(upload, delete, edit). The runner is injected so the parsing/formatting is
unit-testable without a real network call or the yt-dlp binary.
"""

from __future__ import annotations

import subprocess
from typing import Callable, Optional

# Tab-separated so titles with spaces/pipes survive; one line per video.
_PRINT_FMT = "%(view_count)s\t%(title)s\t%(id)s"


def _default_runner(args: list[str], timeout: float = 90.0) -> str:
    """Run yt-dlp and return its stdout. Raises FileNotFoundError if it's missing."""
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return proc.stdout or ""


def fetch_channel_views(channel_id: str, yt_dlp_bin: str = "yt-dlp", *,
                        tab: str = "shorts", limit: int = 30,
                        runner: Optional[Callable] = None) -> list[dict]:
    """Return ``[{"views": int|None, "title": str, "id": str}, ...]`` for a channel.

    ``--flat-playlist`` keeps it fast (one listing call, no per-video extraction);
    a non-numeric view count (e.g. 'NA') becomes ``None`` rather than crashing.
    """
    runner = runner or _default_runner
    tab = tab if tab in ("shorts", "videos") else "shorts"
    url = f"https://www.youtube.com/channel/{channel_id}/{tab}"
    args = [yt_dlp_bin, "--flat-playlist", "--no-warnings",
            "--playlist-end", str(max(1, int(limit))),
            "--print", _PRINT_FMT, url]
    out = runner(args) or ""
    rows: list[dict] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            views = int(parts[0].strip())
        except (TypeError, ValueError):
            views = None
        rows.append({"views": views, "title": parts[1].strip(), "id": parts[2].strip()})
    return rows


def format_views_table(rows: list[dict]) -> str:
    """A compact, highest-views-first table with a total. Pure and testable."""
    if not rows:
        return "No videos found."
    ranked = sorted(rows, key=lambda r: (r["views"] is None, -(r["views"] or 0)))
    lines = []
    for r in ranked:
        views = "?" if r["views"] is None else f"{r['views']:,}"
        lines.append(f"{views:>8}  {r['title']}")
    total = sum(r["views"] for r in rows if isinstance(r["views"], int))
    lines.append(f"\n{len(rows)} videos, {total:,} total views")
    return "\n".join(lines)
