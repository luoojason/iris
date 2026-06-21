# Buffer Multi-Platform Publishing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Iris's per-platform social publishing (YouTube + Instagram direct APIs) with a single Buffer GraphQL client so Iris can publish a finished video to all connected platforms.

**Architecture:** A new `iris/buffer.py` module holds all Buffer logic (GraphQL transport, channel listing/resolution, post creation, an orchestrator, and a permanent-URL media host), built like the existing `iris/social.py` with the HTTP layer injected so it is unit-testable with no Buffer account. `iris/mcp/publish_server.py` is rewritten to call it. `iris/social.py` and its test are deleted.

**Tech Stack:** Python 3.10+, Buffer GraphQL API (personal-token auth), `requests` (lazy), `boto3` (lazy, R2/S3 media hosting), FastMCP, pytest.

## Global Constraints

- Python >= 3.10 (`from __future__ import annotations` at the top of every module).
- HTTP is always injected as `http` (defaults to `requests` via a lazy `_default_http()`); no module-level network. Same for `boto3` (lazy import inside the host function only).
- No new third-party dependency: reuse the existing `publish` extra (`requests>=2`, `boto3>=1.26`).
- Single secret: `IRIS_BUFFER_TOKEN` (a personal API token). No multi-key token object.
- Media URLs handed to Buffer must be **permanent and public** (no presigned/expiring URLs); Buffer fetches media at publish time.
- Publishing is fail-soft per channel: one channel's failure never stops the others, mirroring the current dispatcher.
- The MCP tool keeps the name `publish_video` and the allowlist entry `mcp__publish__publish_video`.
- Keep the `IRIS_PUBLISH_DIR` file restriction in the MCP tool.
- Commit messages: plain imperative, no AI-authorship markers, no emojis.
- After every task the full suite (`python -m pytest -q`) must pass.

## File Structure

- `iris/buffer.py` (create) — all Buffer logic: `BufferError`, `_graphql`, `load_token`, `list_channels`, `resolve_channels`, `create_post`, `publish`, `stable_media_host`, `MediaHost` type.
- `iris/mcp/publish_server.py` (rewrite) — MCP `publish_video` tool calling `iris.buffer.publish`.
- `iris/social.py` (delete in Task 6) — superseded entirely by `iris/buffer.py`.
- `tests/test_buffer.py` (create) — unit tests for `iris/buffer.py`.
- `tests/test_publish_server.py` (rewrite in Task 5) — tests for the rewritten tool.
- `tests/test_social.py` (delete in Task 6).
- `docs/PUBLISHING-SETUP.md` (rewrite in Task 6) — Buffer setup.
- `.env.example` (modify in Task 6) — Buffer env vars.

---

### Task 1: Buffer transport, token, and channels

**Files:**
- Create: `iris/buffer.py`
- Test: `tests/test_buffer.py`

**Interfaces:**
- Produces:
  - `class BufferError(RuntimeError)`
  - `BUFFER_API_URL: str` (module constant)
  - `load_token() -> str` — reads `IRIS_BUFFER_TOKEN` from env, `""` if unset.
  - `_graphql(query: str, variables: dict, *, token: str, http=None) -> dict` — returns the `data` object; raises `BufferError` on a GraphQL `errors` array, an empty/missing `data`, or a transport exception.
  - `list_channels(*, token: str, http=None) -> list[dict]` — returns `[{"id": str, "service": str, "handle": str}, ...]`.
  - `resolve_channels(names: list[str], channels: list[dict]) -> tuple[list[str], list[str]]` — returns `(channel_ids, unknown_names)`; empty `names` returns all channel ids and no unknowns; matching is case-insensitive against `service` and `handle`.

> **Implementer note:** Confirm the exact GraphQL endpoint URL and the channels query field path against developers.buffer.com before finishing. The code below uses `BUFFER_API_URL = "https://graph.buffer.com"` and a `{ account { channels { id service handle } } }`-style shape; adjust the query string and the parse path in `list_channels` together if the real schema differs. The unit tests assert behavior given a fixed response shape, so keep `list_channels` parsing aligned with the query you send.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_buffer.py`:

```python
"""Tests for the Buffer publishing client. No network: HTTP is faked."""

from __future__ import annotations

import pytest

from iris.buffer import (
    BufferError,
    _graphql,
    list_channels,
    load_token,
    resolve_channels,
)


class FakeResp:
    def __init__(self, json_data=None, status_code=200, text=""):
        self._json = json_data or {}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._json


class FakeHttp:
    """Records POSTs and returns queued responses (Buffer is POST-only)."""

    def __init__(self, posts=()):
        self.post_q = list(posts)
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return self.post_q.pop(0)


def test_load_token_from_env(monkeypatch):
    monkeypatch.setenv("IRIS_BUFFER_TOKEN", "tok123")
    assert load_token() == "tok123"
    monkeypatch.delenv("IRIS_BUFFER_TOKEN", raising=False)
    assert load_token() == ""


def test_graphql_returns_data():
    http = FakeHttp(posts=[FakeResp({"data": {"ok": 1}})])
    out = _graphql("query { ok }", {}, token="t", http=http)
    assert out == {"ok": 1}
    # token goes on the Authorization header as a Bearer token
    _, kw = http.calls[0]
    assert "Bearer t" in kw["headers"]["Authorization"]


def test_graphql_raises_on_errors():
    http = FakeHttp(posts=[FakeResp({"errors": [{"message": "bad query"}]})])
    with pytest.raises(BufferError) as exc:
        _graphql("query { x }", {}, token="t", http=http)
    assert "bad query" in str(exc.value)


def test_graphql_raises_on_empty_data():
    http = FakeHttp(posts=[FakeResp({})])
    with pytest.raises(BufferError):
        _graphql("query { x }", {}, token="t", http=http)


def test_list_channels_parses():
    resp = FakeResp({"data": {"account": {"channels": [
        {"id": "c1", "service": "twitter", "handle": "@me"},
        {"id": "c2", "service": "linkedin", "handle": "me"},
    ]}}})
    chans = list_channels(token="t", http=FakeHttp(posts=[resp]))
    assert chans == [
        {"id": "c1", "service": "twitter", "handle": "@me"},
        {"id": "c2", "service": "linkedin", "handle": "me"},
    ]


CHANS = [
    {"id": "c1", "service": "twitter", "handle": "@me"},
    {"id": "c2", "service": "linkedin", "handle": "me"},
    {"id": "c3", "service": "youtube", "handle": "mychan"},
]


def test_resolve_channels_empty_means_all():
    ids, unknown = resolve_channels([], CHANS)
    assert ids == ["c1", "c2", "c3"]
    assert unknown == []


def test_resolve_channels_subset_by_service_case_insensitive():
    ids, unknown = resolve_channels(["Twitter", "LINKEDIN"], CHANS)
    assert ids == ["c1", "c2"]
    assert unknown == []


def test_resolve_channels_reports_unknown():
    ids, unknown = resolve_channels(["twitter", "tiktok"], CHANS)
    assert ids == ["c1"]
    assert unknown == ["tiktok"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/Desktop/iris && python -m pytest tests/test_buffer.py -q`
Expected: FAIL (collection error / `ModuleNotFoundError: No module named 'iris.buffer'`).

- [ ] **Step 3: Write the minimal implementation**

Create `iris/buffer.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/Desktop/iris && python -m pytest tests/test_buffer.py -q`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/iris
git add iris/buffer.py tests/test_buffer.py
git commit -m "Add Buffer GraphQL transport, token, and channel listing"
```

---

### Task 2: Create a post (now or scheduled)

**Files:**
- Modify: `iris/buffer.py`
- Test: `tests/test_buffer.py`

**Interfaces:**
- Consumes: `_graphql`, `BufferError` from Task 1.
- Produces:
  - `create_post(text: str, channel_id: str, *, video_url: Optional[str] = None, scheduled_at: Optional[str] = None, token: str, http=None) -> dict` — runs the `createPost` mutation for one channel. Returns `{"id": <post id>}` on success or `{"error": <reason>}` on failure (fail-soft; never raises). When `video_url` is set, attaches it as a video asset. When `scheduled_at` (an ISO 8601 string) is set, schedules; otherwise posts now.

> **Implementer note:** Confirm the `createPost` input field names (`channelIds`, `assets`, `video`/`thumbnailUrl`, `scheduledAt`, and the "post now" signal) against the Buffer schema. Keep the variables dict the tests assert (`channelIds`, `assets[0].video.url`, `scheduledAt`) aligned with what you send.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_buffer.py`:

```python
from iris.buffer import create_post


def test_create_post_now_with_video():
    http = FakeHttp(posts=[FakeResp({"data": {"createPost": {"id": "p1"}}})])
    out = create_post("hello", "c1", video_url="https://h/v.mp4", token="t", http=http)
    assert out == {"id": "p1"}
    _, kw = http.calls[0]
    variables = kw["json"]["variables"]
    assert variables["input"]["channelIds"] == ["c1"]
    assert variables["input"]["assets"][0]["video"]["url"] == "https://h/v.mp4"
    assert variables["input"].get("scheduledAt") is None


def test_create_post_scheduled():
    http = FakeHttp(posts=[FakeResp({"data": {"createPost": {"id": "p2"}}})])
    out = create_post(
        "hi", "c1", video_url="https://h/v.mp4",
        scheduled_at="2026-07-01T15:00:00", token="t", http=http,
    )
    assert out == {"id": "p2"}
    _, kw = http.calls[0]
    assert kw["json"]["variables"]["input"]["scheduledAt"] == "2026-07-01T15:00:00"


def test_create_post_error_is_failsoft():
    http = FakeHttp(posts=[FakeResp({"errors": [{"message": "channel down"}]})])
    out = create_post("hi", "c1", video_url="https://h/v.mp4", token="t", http=http)
    assert "error" in out and "channel down" in out["error"]


def test_create_post_missing_id_is_error():
    http = FakeHttp(posts=[FakeResp({"data": {"createPost": {}}})])
    out = create_post("hi", "c1", token="t", http=http)
    assert "error" in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/Desktop/iris && python -m pytest tests/test_buffer.py -k create_post -q`
Expected: FAIL (`ImportError: cannot import name 'create_post'`).

- [ ] **Step 3: Write the minimal implementation**

Add to `iris/buffer.py` (after `resolve_channels`):

```python
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
    post_id = (data.get("createPost") or {}).get("id")
    if not post_id:
        return {"error": f"Buffer createPost returned no id: {data}"}
    return {"id": post_id}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/Desktop/iris && python -m pytest tests/test_buffer.py -k create_post -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/iris
git add iris/buffer.py tests/test_buffer.py
git commit -m "Add Buffer createPost for now and scheduled video posts"
```

---

### Task 3: Permanent-URL media host

**Files:**
- Modify: `iris/buffer.py`
- Test: `tests/test_buffer.py`

**Interfaces:**
- Consumes: `BufferError`, `MediaHost` from Task 1.
- Produces:
  - `stable_media_host(*, bucket: Optional[str] = None, endpoint: Optional[str] = None, public_base: Optional[str] = None) -> MediaHost` — returns a function `host(mp4_path: str) -> str` that uploads to a public R2/S3 bucket and returns a **permanent** public URL (`public_base/key`). Raises `BufferError` immediately if no `public_base` is available (presigned URLs are unusable because Buffer fetches at publish time). Reads `IRIS_MEDIA_BUCKET`, `IRIS_MEDIA_ENDPOINT`, `IRIS_MEDIA_PUBLIC_BASE` from env when args are not passed.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_buffer.py`:

```python
from iris.buffer import stable_media_host


def test_stable_media_host_requires_public_base(monkeypatch):
    monkeypatch.setenv("IRIS_MEDIA_BUCKET", "b")
    monkeypatch.delenv("IRIS_MEDIA_PUBLIC_BASE", raising=False)
    with pytest.raises(BufferError) as exc:
        stable_media_host()
    assert "public" in str(exc.value).lower()


def test_stable_media_host_requires_bucket(monkeypatch):
    monkeypatch.delenv("IRIS_MEDIA_BUCKET", raising=False)
    monkeypatch.setenv("IRIS_MEDIA_PUBLIC_BASE", "https://cdn.example.com")
    with pytest.raises(BufferError):
        stable_media_host()


def test_stable_media_host_returns_permanent_url(monkeypatch):
    monkeypatch.setenv("IRIS_MEDIA_BUCKET", "b")
    monkeypatch.setenv("IRIS_MEDIA_PUBLIC_BASE", "https://cdn.example.com/")
    uploaded = {}

    class FakeS3:
        def upload_file(self, path, bucket, key, ExtraArgs=None):
            uploaded["args"] = (path, bucket, key, ExtraArgs)

    host = stable_media_host(uploader=FakeS3())
    url = host("/tmp/clip.mp4")
    assert url.startswith("https://cdn.example.com/")
    assert url.endswith("clip.mp4")
    assert uploaded["args"][1] == "b"
    assert uploaded["args"][3] == {"ContentType": "video/mp4"}
```

> Note: the test injects a fake S3 client through an `uploader=` parameter so no real `boto3`/network is touched. Add that parameter to the implementation.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/Desktop/iris && python -m pytest tests/test_buffer.py -k media_host -q`
Expected: FAIL (`ImportError: cannot import name 'stable_media_host'`).

- [ ] **Step 3: Write the minimal implementation**

Add to `iris/buffer.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/Desktop/iris && python -m pytest tests/test_buffer.py -k media_host -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/iris
git add iris/buffer.py tests/test_buffer.py
git commit -m "Add permanent-URL media host for Buffer video posts"
```

---

### Task 4: Publish orchestrator

**Files:**
- Modify: `iris/buffer.py`
- Test: `tests/test_buffer.py`

**Interfaces:**
- Consumes: `list_channels`, `resolve_channels`, `create_post`, `MediaHost` from Tasks 1-3.
- Produces:
  - `publish(mp4_path: str, caption: str, platforms: list[str], *, scheduled_at: Optional[str] = None, token: str, http=None, media_host: MediaHost) -> dict[str, dict]` — resolves channels (empty `platforms` = all), hosts the video **once**, then calls `create_post` **once per channel**. Returns `{channel_label: {"id"|"error"}}` where `channel_label` is the channel's `service`. Unknown platform names appear as `{name: {"error": "no connected channel named ..."}}`. If hosting fails, every targeted channel gets that error and no posts are created.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_buffer.py`:

```python
from iris.buffer import publish

CHAN_RESP = FakeResp({"data": {"account": {"channels": [
    {"id": "c1", "service": "twitter", "handle": "@me"},
    {"id": "c2", "service": "linkedin", "handle": "me"},
]}}})


def _host_ok(path):
    return "https://cdn.example.com/iris/v.mp4"


def test_publish_all_channels_failsoft():
    # 1 channels query + 2 create_post calls (one ok, one error)
    http = FakeHttp(posts=[
        CHAN_RESP,
        FakeResp({"data": {"createPost": {"id": "p1"}}}),
        FakeResp({"errors": [{"message": "boom"}]}),
    ])
    out = publish("/tmp/v.mp4", "cap", [], token="t", http=http, media_host=_host_ok)
    assert out["twitter"] == {"id": "p1"}
    assert "error" in out["linkedin"] and "boom" in out["linkedin"]["error"]


def test_publish_subset():
    http = FakeHttp(posts=[
        CHAN_RESP,
        FakeResp({"data": {"createPost": {"id": "p1"}}}),
    ])
    out = publish("/tmp/v.mp4", "cap", ["twitter"], token="t", http=http, media_host=_host_ok)
    assert list(out.keys()) == ["twitter"]
    assert out["twitter"] == {"id": "p1"}


def test_publish_unknown_platform_reported():
    http = FakeHttp(posts=[
        CHAN_RESP,
        FakeResp({"data": {"createPost": {"id": "p1"}}}),
    ])
    out = publish("/tmp/v.mp4", "cap", ["twitter", "tiktok"], token="t", http=http, media_host=_host_ok)
    assert out["twitter"] == {"id": "p1"}
    assert "error" in out["tiktok"]


def test_publish_hosting_failure_stops_posts():
    def bad_host(path):
        raise BufferError("no media host")

    http = FakeHttp(posts=[CHAN_RESP])  # only the channels query is consumed
    out = publish("/tmp/v.mp4", "cap", ["twitter"], token="t", http=http, media_host=bad_host)
    assert "error" in out["twitter"] and "no media host" in out["twitter"]["error"]
    assert len(http.calls) == 1  # no create_post attempted
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/Desktop/iris && python -m pytest tests/test_buffer.py -k publish -q`
Expected: FAIL (`ImportError: cannot import name 'publish'`).

- [ ] **Step 3: Write the minimal implementation**

Add to `iris/buffer.py`:

```python
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
        results[label] = create_post(
            caption, cid, video_url=video_url, scheduled_at=scheduled_at, token=token, http=http,
        )
    return results
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/Desktop/iris && python -m pytest tests/test_buffer.py -q`
Expected: PASS (whole file green; ~19 tests).

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/iris
git add iris/buffer.py tests/test_buffer.py
git commit -m "Add Buffer publish orchestrator with per-channel fail-soft"
```

---

### Task 5: Rewrite the MCP publish tool

**Files:**
- Rewrite: `iris/mcp/publish_server.py`
- Rewrite: `tests/test_publish_server.py`

**Interfaces:**
- Consumes: `iris.buffer.load_token`, `iris.buffer.publish`, `iris.buffer.stable_media_host`, `iris.buffer.BufferError`.
- Produces:
  - MCP tool `publish_video(mp4_path: str, caption: str, platforms: str = "", when: str = "now") -> str` — `platforms` is a comma-separated subset (empty = all connected channels); `when` is `"now"` (or empty) or an ISO 8601 datetime to schedule. Keeps the `IRIS_PUBLISH_DIR` restriction and missing-file handling. Formats one result line per channel.

- [ ] **Step 1: Write the failing tests**

Replace the entire contents of `tests/test_publish_server.py` with:

```python
"""Tests for the publish MCP tool (the underlying Buffer client is faked)."""

from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from iris.mcp import publish_server as ps


def test_publish_tool_missing_file():
    assert "No such file" in ps.publish_video("/nope/x.mp4", "cap")


def test_publish_tool_formats_results(tmp_path, monkeypatch):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    monkeypatch.delenv("IRIS_PUBLISH_DIR", raising=False)
    monkeypatch.setattr(ps, "load_token", lambda: "tok")
    monkeypatch.setattr(ps, "stable_media_host", lambda: (lambda p: "https://cdn/v.mp4"))
    monkeypatch.setattr(
        ps, "publish",
        lambda *a, **k: {"twitter": {"id": "p1"}, "linkedin": {"error": "boom"}},
    )
    out = ps.publish_video(str(f), "cap")
    assert "twitter: published p1" in out
    assert "linkedin: FAILED — boom" in out


def test_publish_tool_missing_token(tmp_path, monkeypatch):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    monkeypatch.delenv("IRIS_PUBLISH_DIR", raising=False)
    monkeypatch.setattr(ps, "load_token", lambda: "")
    out = ps.publish_video(str(f), "cap")
    assert "IRIS_BUFFER_TOKEN" in out


def test_publish_tool_bad_when(tmp_path, monkeypatch):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    monkeypatch.delenv("IRIS_PUBLISH_DIR", raising=False)
    monkeypatch.setattr(ps, "load_token", lambda: "tok")
    out = ps.publish_video(str(f), "cap", when="not-a-date")
    assert "when" in out.lower() or "date" in out.lower()


def test_publish_tool_passes_schedule(tmp_path, monkeypatch):
    f = tmp_path / "v.mp4"
    f.write_bytes(b"x")
    monkeypatch.delenv("IRIS_PUBLISH_DIR", raising=False)
    monkeypatch.setattr(ps, "load_token", lambda: "tok")
    monkeypatch.setattr(ps, "stable_media_host", lambda: (lambda p: "https://cdn/v.mp4"))
    seen = {}

    def fake_publish(mp4_path, caption, platforms, **k):
        seen["scheduled_at"] = k.get("scheduled_at")
        seen["platforms"] = platforms
        return {"twitter": {"id": "p1"}}

    monkeypatch.setattr(ps, "publish", fake_publish)
    ps.publish_video(str(f), "cap", platforms="twitter", when="2026-07-01T15:00:00")
    assert seen["scheduled_at"] == "2026-07-01T15:00:00"
    assert seen["platforms"] == ["twitter"]


def test_publish_dir_restriction(tmp_path, monkeypatch):
    inside = tmp_path / "out"
    inside.mkdir()
    good = inside / "v.mp4"
    good.write_bytes(b"x")
    outside = tmp_path / "other.mp4"
    outside.write_bytes(b"x")
    monkeypatch.setenv("IRIS_PUBLISH_DIR", str(inside))
    monkeypatch.setattr(ps, "load_token", lambda: "tok")
    monkeypatch.setattr(ps, "stable_media_host", lambda: (lambda p: "https://cdn/v.mp4"))
    monkeypatch.setattr(ps, "publish", lambda *a, **k: {"twitter": {"id": "p1"}})
    assert "Refused" in ps.publish_video(str(outside), "cap")
    assert "Refused" not in ps.publish_video(str(good), "cap")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/Desktop/iris && python -m pytest tests/test_publish_server.py -q`
Expected: FAIL (the old `publish_server` imports `SocialTokens`/`_publish_video`; the new tests reference `load_token`/`publish`/`stable_media_host` which do not exist there yet).

- [ ] **Step 3: Write the minimal implementation**

Replace the entire contents of `iris/mcp/publish_server.py` with:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd ~/Desktop/iris && python -m pytest tests/test_publish_server.py -q`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
cd ~/Desktop/iris
git add iris/mcp/publish_server.py tests/test_publish_server.py
git commit -m "Rewrite publish MCP tool on the Buffer client"
```

---

### Task 6: Remove the old direct publishers and update docs

**Files:**
- Delete: `iris/social.py`
- Delete: `tests/test_social.py`
- Rewrite: `docs/PUBLISHING-SETUP.md`
- Modify: `.env.example`

**Interfaces:**
- Consumes: nothing new. Verifies nothing else imports `iris.social`.

- [ ] **Step 1: Confirm nothing imports the old module**

Run: `cd ~/Desktop/iris && grep -rn "iris.social\|from .social\|import social" iris/ tests/`
Expected: no matches (Task 5 removed the last importer). If any remain, fix them to use `iris.buffer` before deleting.

- [ ] **Step 2: Delete the old module and its test**

```bash
cd ~/Desktop/iris
git rm iris/social.py tests/test_social.py
```

- [ ] **Step 3: Rewrite the setup doc**

Replace the entire contents of `docs/PUBLISHING-SETUP.md` with:

```markdown
# Publishing setup (Buffer)

Iris publishes finished videos to your own social channels through Buffer's
GraphQL API. Single user only: this posts to your own connected channels.

## 1. Buffer

1. Create a Buffer account and connect the channels you want (X, LinkedIn,
   Instagram, YouTube, Threads, Bluesky, Pinterest, Facebook, etc.). A paid plan
   is needed for more than 3 channels.
2. Generate a personal API token (developers.buffer.com) and set it:

   ```
   IRIS_BUFFER_TOKEN=your-personal-token
   ```

## 2. Permanent media hosting

Buffer fetches your video at publish time (possibly days later for scheduled
posts), so the video must live at a permanent, public, direct HTTPS URL.
Presigned/expiring URLs fail silently. Use a public Cloudflare R2 bucket (or any
host that serves files at a stable public URL):

```
IRIS_MEDIA_BUCKET=your-bucket
IRIS_MEDIA_ENDPOINT=https://<account>.r2.cloudflarestorage.com   # omit for AWS S3
IRIS_MEDIA_PUBLIC_BASE=https://media.yourdomain.com              # the public base the bucket serves at
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

`IRIS_MEDIA_PUBLIC_BASE` is required; without it publishing refuses to run.

## 3. Wire the tool

Add the publish server to `mcp.json` and allowlist `mcp__publish__publish_video`.
Optionally set `IRIS_PUBLISH_DIR` to restrict which files can be published.

## 4. Use

Ask Iris to publish a clip. It posts to all connected channels by default, or a
named subset ("publish to x and linkedin"), now or scheduled
("publish at 2026-07-01T15:00:00").

## Notes

- TikTok: Buffer lists it as a channel, but beta video-post support is not
  guaranteed; a channel that rejects the post is reported and the others still
  publish.
- The first real post is the real test; expect to confirm channel-by-channel.
```

- [ ] **Step 4: Update `.env.example`**

In `.env.example`, remove any `IRIS_SOCIAL_TOKENS` / YouTube / Instagram token lines and the presigned-only media notes, and ensure these lines are present (add them under a "Publishing (Buffer)" comment if not):

```
# Publishing (Buffer)
IRIS_BUFFER_TOKEN=
IRIS_MEDIA_BUCKET=
IRIS_MEDIA_ENDPOINT=
IRIS_MEDIA_PUBLIC_BASE=
```

- [ ] **Step 5: Run the full suite**

Run: `cd ~/Desktop/iris && python -m pytest -q`
Expected: PASS (no `test_social.py`; `test_buffer.py` and `test_publish_server.py` green; total count unchanged or higher minus the removed social tests).

- [ ] **Step 6: Commit**

```bash
cd ~/Desktop/iris
git add -A
git commit -m "Remove direct YouTube/Instagram publishers; document Buffer setup"
```

---

## Notes for the implementer

- The Buffer GraphQL endpoint, the channels query field path, and the `createPost` input field names are in public beta and must be confirmed against developers.buffer.com (see the implementer notes in Tasks 1 and 2). The unit tests fix a response/variable shape; if the real schema differs, change the query/mutation string and the matching parse/variable code together so the tests still describe real behavior.
- Everything here builds and passes with no Buffer account. Live verification (a real post to a real channel) happens after setup, per the setup doc.
