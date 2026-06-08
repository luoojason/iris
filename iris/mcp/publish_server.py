"""MCP server: publish a finished video to the owner's own social accounts.

Exposes one tool, ``publish_video``, that posts to YouTube (Shorts) and Instagram
(Reels) via their official APIs. TikTok is intentionally absent until its audit
path is wired. Tokens load from IRIS_SOCIAL_TOKENS (a 600 JSON file on the box).
Allowlist ``mcp__publish__publish_video`` and tell the persona it can publish.
"""

from __future__ import annotations

import os

from ..social import PublishError, SocialTokens, s3_media_host
from ..social import publish_video as _publish_video

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
def publish_video(mp4_path: str, caption: str, platforms: str = "youtube,instagram", privacy: str = "unlisted") -> str:
    """Publish a finished video to social platforms.

    Defaults to ``unlisted`` so a video only goes fully public when the caller
    asks for it. If ``IRIS_PUBLISH_DIR`` is set, only files inside it can be
    published.

    Args:
        mp4_path: Absolute path to the .mp4 to publish.
        caption: Caption / title / description for the post.
        platforms: Comma-separated: any of youtube, instagram.
        privacy: public | unlisted | private (YouTube; Instagram is always public).
    """
    if not os.path.isfile(mp4_path):
        return f"No such file: {mp4_path}"
    if not _within_publish_dir(mp4_path):
        return f"Refused: {mp4_path} is outside IRIS_PUBLISH_DIR."
    tokens = SocialTokens.load()
    wanted = [p.strip().lower() for p in platforms.split(",") if p.strip()]
    host = None
    if "instagram" in wanted:
        try:
            host = s3_media_host()
        except PublishError:
            host = None  # the dispatcher reports the missing-host error per-platform
    results = _publish_video(mp4_path, caption, wanted, tokens=tokens, privacy=privacy, media_host=host)
    lines = []
    for platform, res in results.items():
        if "error" in res:
            lines.append(f"{platform}: FAILED — {res['error']}")
        else:
            lines.append(f"{platform}: published {res.get('url') or res.get('id')}")
    return "\n".join(lines) or "Nothing published."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
