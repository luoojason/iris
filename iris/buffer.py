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
    except Exception as exc:  # network
        raise BufferError(f"Buffer request failed: {exc}") from exc
    status = getattr(resp, "status_code", 200)
    if status >= 400:
        body_text = (getattr(resp, "text", "") or "")[:200]
        raise BufferError(f"Buffer HTTP {status}: {body_text}")
    try:
        body = resp.json()
    except Exception as exc:  # non-JSON response
        raise BufferError(f"Buffer returned non-JSON ({status}): {exc}") from exc
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


def stable_media_host(
    *,
    bucket: Optional[str] = None,
    endpoint: Optional[str] = None,
    public_base: Optional[str] = None,
    uploader=None,
) -> MediaHost:
    """A media host returning a PERMANENT public URL (never presigned).

    Buffer fetches media at publish time (possibly days later for scheduled
    posts), so an expiring URL fails silently. This requires a public base URL
    (a public R2/S3 bucket or CDN domain) and refuses to run without one.

    Env: IRIS_MEDIA_BUCKET, IRIS_MEDIA_ENDPOINT (R2/B2 endpoint, omit for AWS),
    IRIS_MEDIA_PUBLIC_BASE (the public base the bucket serves files at), plus the
    usual AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY for the upload.
    """
    bucket = bucket or os.environ.get("IRIS_MEDIA_BUCKET")
    endpoint = endpoint or os.environ.get("IRIS_MEDIA_ENDPOINT") or None
    public_base = public_base or os.environ.get("IRIS_MEDIA_PUBLIC_BASE") or None
    if not bucket:
        raise BufferError("set IRIS_MEDIA_BUCKET (and S3/R2 credentials) to host videos")
    if not public_base:
        raise BufferError(
            "set IRIS_MEDIA_PUBLIC_BASE to a permanent public URL base; Buffer "
            "fetches media at publish time, so presigned/expiring URLs fail"
        )

    def host(mp4_path: str) -> str:
        client = uploader
        if client is None:
            import boto3  # lazy: only when actually posting

            client = boto3.client("s3", endpoint_url=endpoint)
        key = f"iris/{int(time.time())}-{os.path.basename(mp4_path)}"
        client.upload_file(mp4_path, bucket, key, ExtraArgs={"ContentType": "video/mp4"})
        return f"{public_base.rstrip('/')}/{key}"

    return host


def publish(
    mp4_path: str,
    caption: str,
    platforms: list[str],
    *,
    scheduled_at: Optional[str] = None,
    token: str,
    http=None,
    media_host: MediaHost,
) -> dict[str, dict]:
    """Publish one video to the named channels (or all). {service: {id|error}}."""
    channels = list_channels(token=token, http=http)
    by_id = {c["id"]: c for c in channels}
    ids, unknown = resolve_channels(platforms, channels)

    results: dict[str, dict] = {}
    for name in unknown:
        results[name] = {"error": f"no connected channel named {name!r}"}

    if not ids:
        return results

    # Host once; reuse the one URL for every channel.
    try:
        video_url = media_host(mp4_path)
    except Exception as exc:
        for cid in ids:
            label = by_id[cid]["service"] or cid
            results[label] = {"error": f"could not host the video: {exc}"}
        return results

    for cid in ids:
        label = by_id[cid]["service"] or cid
        try:
            results[label] = create_post(
                caption, cid, video_url=video_url, scheduled_at=scheduled_at, token=token, http=http,
            )
        except Exception as exc:  # one channel must never sink the rest
            results[label] = {"error": str(exc)}
    return results
