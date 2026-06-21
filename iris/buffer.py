"""Publish a finished video to the owner's own social accounts, via Buffer.

One integration replaces per-platform code. Buffer's GraphQL API authenticates
with a personal API token (the single-user path), and covers ~11 platforms. The
HTTP layer is injected (`http`) so the whole module is unit-testable without
network or a real account. In production it is the ``requests`` library.

Auth: a single personal token in IRIS_BUFFER_TOKEN.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Optional

# Confirm against developers.buffer.com; the schema/endpoint is in public beta.
BUFFER_API_URL = "https://graph.buffer.com"

# A function that takes a local mp4 path and returns a permanent public HTTPS URL.
MediaHost = Callable[[str], str]


class BufferError(RuntimeError):
    pass


def _default_http():
    import requests  # lazy: only needed for real posting

    return requests


def load_token() -> str:
    return os.environ.get("IRIS_BUFFER_TOKEN", "")


def _graphql(query: str, variables: dict, *, token: str, http=None) -> dict:
    """Run one GraphQL operation. Returns the ``data`` object or raises."""
    http = http or _default_http()
    try:
        resp = http.post(
            BUFFER_API_URL,
            json={"query": query, "variables": variables},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=60,
        )
        body = resp.json()
    except Exception as exc:  # network / decode
        raise BufferError(f"Buffer request failed: {exc}") from exc
    if body.get("errors"):
        msgs = "; ".join(e.get("message", str(e)) for e in body["errors"])
        raise BufferError(msgs)
    data = body.get("data")
    if not data:
        raise BufferError(f"Buffer returned no data: {body}")
    return data


def list_channels(*, token: str, http=None) -> list[dict]:
    """Return connected channels as [{id, service, handle}]."""
    query = "query { account { channels { id service handle } } }"
    data = _graphql(query, {}, token=token, http=http)
    raw = (data.get("account") or {}).get("channels") or []
    return [
        {"id": c.get("id", ""), "service": c.get("service", ""), "handle": c.get("handle", "")}
        for c in raw
    ]


def resolve_channels(names: list[str], channels: list[dict]) -> tuple[list[str], list[str]]:
    """Map requested names to channel ids. Empty names selects all channels."""
    if not names:
        return [c["id"] for c in channels], []
    ids: list[str] = []
    unknown: list[str] = []
    for name in names:
        key = name.strip().lower()
        match = next(
            (c for c in channels if key in (c["service"].lower(), c["handle"].lower())),
            None,
        )
        if match:
            ids.append(match["id"])
        else:
            unknown.append(name.strip())
    return ids, unknown


_CREATE_POST = (
    "mutation($input: CreatePostInput!) { createPost(input: $input) { id } }"
)


def create_post(
    text: str,
    channel_id: str,
    *,
    video_url: Optional[str] = None,
    scheduled_at: Optional[str] = None,
    token: str,
    http=None,
) -> dict:
    """Create one post on one channel. {id} or {error}; never raises."""
    inp: dict = {"text": text, "channelIds": [channel_id]}
    if video_url:
        inp["assets"] = [{"video": {"url": video_url}}]
    if scheduled_at:
        inp["scheduledAt"] = scheduled_at
    else:
        inp["scheduledAt"] = None  # null => post now
    try:
        data = _graphql(_CREATE_POST, {"input": inp}, token=token, http=http)
    except BufferError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # honor the never-raises contract
        return {"error": f"unexpected: {exc}"}
    post_id = (data.get("createPost") or {}).get("id")
    if not post_id:
        return {"error": f"Buffer createPost returned no id: {data}"}
    return {"id": post_id}
