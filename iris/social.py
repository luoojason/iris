"""Publish a finished video to the owner's own social accounts.

Direct, official-API publishing for YouTube (Shorts) and Instagram (Reels). Both
are sanctioned programmatic-posting paths; no scraping, no third party. TikTok is
deliberately not here yet (its API gates public posting behind an app audit that
needs a per-post consent UI a headless agent does not have).

The HTTP layer is injected (`http`) so the whole module is unit-testable without
network or real accounts. In production it is the ``requests`` library.

Tokens come from a small JSON file owned by the bot (perms 600), never the repo:
  { "yt_client_id", "yt_client_secret", "yt_refresh_token",
    "ig_user_id", "ig_access_token" }
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Callable, Optional

GRAPH = "https://graph.instagram.com/v22.0"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YT_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

# A function that takes a local mp4 path and returns a public HTTPS URL Meta can
# fetch. Instagram requires this; the box has no built-in public URL.
MediaHost = Callable[[str], str]


class PublishError(RuntimeError):
    pass


def _default_http():
    import requests  # lazy: only needed for real posting

    return requests


@dataclass
class SocialTokens:
    yt_client_id: str = ""
    yt_client_secret: str = ""
    yt_refresh_token: str = ""
    ig_user_id: str = ""
    ig_access_token: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "SocialTokens":
        return cls(**{k: data.get(k, "") for k in cls().__dict__})

    @classmethod
    def load(cls, path: Optional[str] = None) -> "SocialTokens":
        path = path or os.environ.get("IRIS_SOCIAL_TOKENS", "iris-social.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return cls.from_dict(json.load(handle))
        except (OSError, json.JSONDecodeError):
            return cls()

    def has_youtube(self) -> bool:
        return bool(self.yt_client_id and self.yt_client_secret and self.yt_refresh_token)

    def has_instagram(self) -> bool:
        return bool(self.ig_user_id and self.ig_access_token)


# --- YouTube (Shorts) -----------------------------------------------------

def youtube_access_token(tokens: SocialTokens, *, http=None) -> str:
    """Exchange the stored refresh token for a fresh access token (headless)."""
    http = http or _default_http()
    resp = http.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": tokens.yt_client_id,
            "client_secret": tokens.yt_client_secret,
            "refresh_token": tokens.yt_refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    body = resp.json()
    token = body.get("access_token")
    if not token:
        raise PublishError(f"YouTube token refresh failed: {body.get('error_description') or body.get('error') or body}")
    return token


def publish_youtube(
    mp4_path: str,
    title: str,
    description: str = "",
    privacy: str = "public",
    category_id: str = "22",  # People & Blogs (a sane generic default; 20 is Gaming)
    *,
    tokens: SocialTokens,
    http=None,
    read_file: Callable[[str], bytes] | None = None,
) -> dict:
    """Upload a video as a Short via a resumable upload. Returns {id, url} or {error}.

    A 9:16, <=3min clip is auto-classified as a Short; ``#Shorts`` in the
    description is a hint. Note: uploads land private until the project's YouTube
    API audit clears, regardless of ``privacy``.
    """
    http = http or _default_http()
    read_file = read_file or (lambda p: open(p, "rb").read())
    try:
        access = youtube_access_token(tokens, http=http)
        data = read_file(mp4_path)
        metadata = {
            "snippet": {"title": title[:100], "description": description, "categoryId": category_id},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
        }
        init = http.post(
            YT_UPLOAD_URL + "?uploadType=resumable&part=snippet,status",
            headers={
                "Authorization": f"Bearer {access}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": "video/*",
                "X-Upload-Content-Length": str(len(data)),
            },
            json=metadata,
            timeout=60,
        )
        location = (getattr(init, "headers", {}) or {}).get("Location") or (getattr(init, "headers", {}) or {}).get("location")
        if not location:
            return {"error": f"YouTube did not return an upload URL (HTTP {getattr(init, 'status_code', '?')}): {_safe_text(init)}"}
        up = http.put(
            location,
            headers={"Authorization": f"Bearer {access}", "Content-Type": "video/*"},
            data=data,
            timeout=600,
        )
        body = up.json()
        vid = body.get("id")
        if not vid:
            return {"error": f"YouTube upload failed: {body.get('error', body)}"}
        return {"id": vid, "url": f"https://youtu.be/{vid}"}
    except PublishError as exc:
        return {"error": str(exc)}
    except Exception as exc:  # network etc.
        return {"error": f"YouTube error: {exc}"}


# --- Instagram (Reels) ----------------------------------------------------

def publish_instagram(
    public_video_url: str,
    caption: str,
    *,
    tokens: SocialTokens,
    http=None,
    sleep: Callable[[float], None] = time.sleep,
    max_poll: int = 30,
) -> dict:
    """Create a Reels container from a public URL, wait for it, publish. {id} or {error}."""
    http = http or _default_http()
    try:
        create = http.post(
            f"{GRAPH}/{tokens.ig_user_id}/media",
            data={
                "media_type": "REELS",
                "video_url": public_video_url,
                "caption": caption,
                "access_token": tokens.ig_access_token,
            },
            timeout=60,
        )
        container = create.json().get("id")
        if not container:
            return {"error": f"Instagram container failed: {create.json().get('error', create.json())}"}
        # Meta downloads and transcodes the video; poll until it is ready.
        for _ in range(max_poll):
            status = http.get(
                f"{GRAPH}/{container}",
                params={"fields": "status_code", "access_token": tokens.ig_access_token},
                timeout=30,
            ).json()
            code = status.get("status_code")
            if code == "FINISHED":
                break
            if code == "ERROR":
                return {"error": f"Instagram transcode error for container {container}"}
            sleep(10)
        else:
            return {"error": "Instagram container did not finish in time"}
        published = http.post(
            f"{GRAPH}/{tokens.ig_user_id}/media_publish",
            data={"creation_id": container, "access_token": tokens.ig_access_token},
            timeout=60,
        ).json()
        media_id = published.get("id")
        if not media_id:
            return {"error": f"Instagram publish failed: {published.get('error', published)}"}
        return {"id": media_id}
    except Exception as exc:
        return {"error": f"Instagram error: {exc}"}


# --- Dispatcher -----------------------------------------------------------

def publish_video(
    mp4_path: str,
    caption: str,
    platforms: list[str],
    *,
    tokens: SocialTokens,
    privacy: str = "public",
    media_host: Optional[MediaHost] = None,
    http=None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Publish one video to the named platforms. Returns {platform: {id|error}}."""
    results: dict[str, dict] = {}
    wanted = {p.lower() for p in platforms}

    if "youtube" in wanted:
        if not tokens.has_youtube():
            results["youtube"] = {"error": "YouTube tokens not configured"}
        else:
            results["youtube"] = publish_youtube(
                mp4_path, title=caption, description=caption, privacy=privacy, tokens=tokens, http=http
            )

    if "instagram" in wanted:
        if not tokens.has_instagram():
            results["instagram"] = {"error": "Instagram tokens not configured"}
        elif media_host is None:
            results["instagram"] = {"error": "Instagram needs a public video URL; no media_host configured"}
        else:
            try:
                url = media_host(mp4_path)
            except Exception as exc:
                results["instagram"] = {"error": f"could not host the video for Instagram: {exc}"}
            else:
                results["instagram"] = publish_instagram(url, caption, tokens=tokens, http=http, sleep=sleep)

    for p in wanted - {"youtube", "instagram"}:
        results[p] = {"error": f"{p} publishing is not implemented yet"}
    return results


def _safe_text(resp) -> str:
    try:
        return (resp.text or "")[:200]
    except Exception:
        return ""


def s3_media_host(*, bucket=None, endpoint=None, public_base=None, expire=3600) -> MediaHost:
    """A media host backed by any S3-compatible store (AWS S3, Cloudflare R2,
    Backblaze B2). Uploads the mp4 and returns a presigned (or public) HTTPS URL
    Meta can fetch. Reads bucket/endpoint/keys from env when not passed.

    Env: IRIS_MEDIA_BUCKET, IRIS_MEDIA_ENDPOINT (R2/B2 endpoint, omit for AWS),
    IRIS_MEDIA_PUBLIC_BASE (if the bucket is public, return base/key instead of
    a presigned URL), plus the usual AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY.
    """
    bucket = bucket or os.environ.get("IRIS_MEDIA_BUCKET")
    endpoint = endpoint or os.environ.get("IRIS_MEDIA_ENDPOINT") or None
    public_base = public_base or os.environ.get("IRIS_MEDIA_PUBLIC_BASE") or None
    if not bucket:
        raise PublishError("set IRIS_MEDIA_BUCKET (and S3/R2 credentials) to host Instagram videos")

    def host(mp4_path: str) -> str:
        import boto3  # lazy: only when Instagram actually posts

        key = f"iris/{int(time.time())}-{os.path.basename(mp4_path)}"
        client = boto3.client("s3", endpoint_url=endpoint)
        client.upload_file(mp4_path, bucket, key, ExtraArgs={"ContentType": "video/mp4"})
        if public_base:
            return f"{public_base.rstrip('/')}/{key}"
        return client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=expire
        )

    return host
