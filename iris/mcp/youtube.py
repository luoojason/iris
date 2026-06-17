"""MCP server: read the channel's public view counts via yt-dlp (no browser).

The simple path for "how many views" — yt-dlp reads the public listing in a few
seconds, no logged-in browser session. Config is read lazily (the claude child
strips IRIS_* from this server's spawn env, so the channel id and yt-dlp path
come from .env at call time).
"""

from __future__ import annotations

import os

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Needs the MCP SDK: pip install 'iris-agent[memory]'") from exc

from iris.youtube import fetch_channel_views, format_views_table

mcp = FastMCP("iris-youtube")


def _config() -> tuple[str, str]:
    from iris.config import load_dotenv

    load_dotenv()
    return (os.environ.get("IRIS_YOUTUBE_CHANNEL", ""),
            os.environ.get("IRIS_YT_DLP_BIN", "yt-dlp"))


@mcp.tool()
def channel_views(limit: int = 30, tab: str = "shorts") -> str:
    """Your channel's public videos and their view counts, highest first.

    Reads them straight from YouTube in a few seconds — no browser, no login. Use
    this for "how many views"; only start a browser job for Studio-only analytics
    (CTR, retention, impressions) or actions (upload, delete, edit).

    Args:
        limit: Most videos to list.
        tab: Which channel tab — 'shorts' (default) or 'videos'.
    """
    channel, yt_dlp_bin = _config()
    if not channel:
        return "No YouTube channel is configured (owner sets IRIS_YOUTUBE_CHANNEL)."
    try:
        rows = fetch_channel_views(channel, yt_dlp_bin, tab=tab, limit=limit)
    except FileNotFoundError:
        return (f"yt-dlp not found at {yt_dlp_bin!r}; the owner can point "
                "IRIS_YT_DLP_BIN at its full path.")
    except Exception as exc:
        return f"Couldn't read the channel's views: {exc}"
    return format_views_table(rows)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
