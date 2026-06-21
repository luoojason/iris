"""MCP server: publish a finished video to the owner's own social accounts.

Exposes one tool, ``publish_video``, that posts to all (or named) connected
Buffer channels via Buffer's GraphQL API. Auth is a single personal token in
IRIS_BUFFER_TOKEN; video is hosted at a permanent public URL (IRIS_MEDIA_*).
Allowlist ``mcp__publish__publish_video`` and tell the persona it can publish.
"""

from __future__ import annotations

import os
from datetime import datetime

from ..buffer import BufferError, load_token, publish, stable_media_host

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Needs the MCP SDK: pip install 'iris-agent[memory]'") from exc

mcp = FastMCP("iris-publish")


def _within_publish_dir(path: str) -> bool:
    """If IRIS_PUBLISH_DIR is set, the file must live inside it.

    Publishing is irreversible and public, so a confused or prompt-injected turn
    should not be able to post any file on the box. Unset = no restriction.
    """
    base = os.environ.get("IRIS_PUBLISH_DIR")
    if not base:
        return True
    base_real = os.path.realpath(base)
    target = os.path.realpath(path)
    return target == base_real or target.startswith(base_real + os.sep)


@mcp.tool()
def publish_video(mp4_path: str, caption: str, platforms: str = "", when: str = "now") -> str:
    """Publish a finished video to social platforms via Buffer.

    Posts to all connected channels by default. If ``IRIS_PUBLISH_DIR`` is set,
    only files inside it can be published.

    Args:
        mp4_path: Absolute path to the .mp4 to publish.
        caption: Caption / title / description for the post.
        platforms: Comma-separated channel names (service or handle); empty = all.
        when: "now" (or empty) to post immediately, or an ISO 8601 datetime
            (e.g. 2026-07-01T15:00:00) to schedule.
    """
    if not os.path.isfile(mp4_path):
        return f"No such file: {mp4_path}"
    if not _within_publish_dir(mp4_path):
        return f"Refused: {mp4_path} is outside IRIS_PUBLISH_DIR."
    token = load_token()
    if not token:
        return "IRIS_BUFFER_TOKEN is not set. See docs/PUBLISHING-SETUP.md."

    scheduled_at = None
    if when and when.strip().lower() != "now":
        try:
            datetime.fromisoformat(when.strip())
        except ValueError:
            return f"Could not parse `when` as a date/time: {when!r}. Use ISO 8601 or 'now'."
        scheduled_at = when.strip()

    try:
        host = stable_media_host()
    except BufferError as exc:
        return f"Media hosting is not configured: {exc}"

    names = [p.strip() for p in platforms.split(",") if p.strip()]
    # http=None lets iris.buffer.publish use its default requests client; injectable in tests
    results = publish(
        mp4_path, caption, names, scheduled_at=scheduled_at, token=token, http=None, media_host=host,
    )
    lines = []
    for channel, res in results.items():
        if "error" in res:
            lines.append(f"{channel}: FAILED — {res['error']}")
        else:
            lines.append(f"{channel}: published {res.get('id')}")
    return "\n".join(lines) or "Nothing published."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
