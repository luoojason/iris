# Buffer Multi-Platform Publishing Design

**Date:** 2026-06-10
**Status:** Approved, ready for implementation plan
**Goal:** Replace Iris's per-platform social publishing (YouTube + Instagram via direct APIs) with Buffer as a single multi-platform publishing layer, so Iris can publish a finished video to all connected platforms instead of just YouTube.

## Background

Iris currently publishes video through `iris/social.py`: separate `publish_youtube` (resumable upload), `publish_instagram` (Reels via a public URL plus status polling), a `publish_video` dispatcher, and `s3_media_host` for the public URL Meta requires. It is exposed as the MCP tool `publish_video` in `iris/mcp/publish_server.py`, guarded by an `IRIS_PUBLISH_DIR` file restriction. TikTok was never wired because its direct API gates public posting behind a per-post consent UI a headless agent lacks.

Buffer's new GraphQL API (public beta, Feb 2026) authenticates with a **personal API key**, which fits Iris's single-user compliance model exactly (the third-party-OAuth path that Buffer keeps restricted is for multi-tenant SaaS, which Iris is not). It covers roughly eleven platforms (Instagram, Facebook, LinkedIn, X, Threads, Bluesky, Pinterest, YouTube, Google Business, Mastodon, and TikTok as a connected channel). One integration replaces the per-platform code.

### Key external facts (verified against developers.buffer.com)

- **Auth:** personal API token, Bearer on the GraphQL endpoint.
- **Posting:** `createPost` mutation handles text, channel targeting, scheduling, and assets.
- **Video:** provided by reference, not upload. `CreatePostInput.assets` takes a `VideoAssetInput` with a `url` (and optional `thumbnailUrl`). There is no upload-to-Buffer endpoint; you host the file yourself.
- **Critical media constraint:** the URL must stay reachable until the post **publishes** (hours or days later for scheduled posts). Buffer fetches the media at publish time. Presigned/expiring URLs (default S3 behavior) "often work the moment you call createPost but expire before the post publishes, causing it to fail silently." The URL must be public, direct, HTTPS, and **permanent**.
- **TikTok:** listed as a connectable channel, but beta video-post support is uncertain and must be confirmed by live test.

## Decisions (from brainstorming)

1. **Replace everything.** Buffer is the single publishing path for all platforms, including YouTube and Instagram. The direct YouTube/Instagram/Google-OAuth/presigned code is removed.
2. **Native client.** A small `iris/buffer.py` client calls Buffer's GraphQL API with the personal key (HTTP injected, unit-testable), exposed through `iris/mcp/publish_server.py`. No dependency on a third-party Buffer MCP.
3. **Post now plus schedule.** The tool supports immediate posting and "publish at a given time."
4. **Default to all connected channels.** With no platforms named, a publish fans out to every connected Buffer channel; a named subset narrows it.

## Architecture

```
publish_video (MCP tool, iris/mcp/publish_server.py)
        |
        v
iris/buffer.py
  - BufferError
  - _graphql(query, variables, *, token, http)   GraphQL transport
  - list_channels(*, token, http)                connected channels
  - resolve_channels(names, channels)            names -> channel ids
  - create_post(text, channel_id, *, video_url, scheduled_at, token, http)
  - publish(mp4_path, caption, platforms, *, scheduled_at, token, http, media_host)
  - stable_media_host(...)                        permanent public URL only
```

`iris/social.py`'s YouTube/Instagram/Google-OAuth/presigned code is deleted. Auth collapses from the multi-key `SocialTokens` to a single `IRIS_BUFFER_TOKEN`. The MCP tool keeps the name `publish_video` and the allowlist entry `mcp__publish__publish_video`, so the persona wiring is unchanged.

## Components (`iris/buffer.py`)

Built in the same style as `social.py`: the HTTP layer is injected (`http`, defaulting to `requests`) so the module is fully unit-testable without network or a real account.

- **`BufferError(RuntimeError)`** for surfaced failures.
- **`_graphql(query, variables, *, token, http)`** POSTs `{query, variables}` to Buffer's GraphQL endpoint with `Authorization: Bearer <token>`. Returns `data` on success; raises `BufferError` if the response carries a GraphQL `errors` array or is unparseable (the beta API can change shape, so this never crashes the turn).
- **`list_channels(*, token, http) -> list[dict]`** queries connected channels and returns `[{id, service, handle}]`.
- **`resolve_channels(names, channels) -> tuple[list[str], list[str]]`** returns `(channel_ids, unknown_names)`. Empty `names` selects all channels. Matching is case-insensitive against service and handle.
- **`create_post(text, channel_id, *, video_url=None, scheduled_at=None, token, http) -> dict`** runs the `createPost` mutation for one channel with a `VideoAssetInput` (`{url, thumbnailUrl?}`) when `video_url` is given, scheduled at `scheduled_at` (ISO) or "share now" when none. Returns `{"id": <post id>}` or `{"error": <reason>}`.
- **`publish(mp4_path, caption, platforms, *, scheduled_at=None, token, http, media_host) -> dict[str, dict]`** orchestrates: resolve channels, host the video **once** to a stable URL, then call `create_post` **once per channel** so each platform's success or failure is isolated. Returns `{channel_label: {"id"|"error"}}`.
- **`stable_media_host(...) -> MediaHost`** uploads the mp4 to a public R2/S3 bucket and returns a **permanent** public URL (the `public_base` path). It refuses to return a presigned/expiring URL, raising `BufferError` with guidance if only presigned hosting is configured, because an expiring URL fails silently at Buffer's publish time.

## Data flow

1. `publish_video(mp4_path, caption, platforms="", when="now")` is called.
2. Validate the file exists and is inside `IRIS_PUBLISH_DIR` (unchanged).
3. Load `IRIS_BUFFER_TOKEN`; clear error if missing.
4. `list_channels` then `resolve_channels`: empty `platforms` means all connected channels, otherwise the named subset; unknown names are surfaced, not dropped.
5. Parse `when`: `"now"` (or empty) posts immediately; otherwise parse an ISO datetime into `scheduled_at`, erroring clearly before anything posts.
6. Host the mp4 once to a stable public URL via `media_host`.
7. For each target channel, `create_post(text=caption, channel_id=id, video_url=url, scheduled_at=...)`. Collect per-channel results.
8. Return formatted per-channel lines, e.g. `x: published <id>` / `tiktok: FAILED — <reason>`.

## Error handling

Fail-soft per channel, matching today's dispatcher: one channel failing never stops the others. Cases:

- Missing `IRIS_BUFFER_TOKEN`: clear setup error, nothing posts.
- No connected channels, or an unknown platform name: reported, not silently dropped.
- Media host unconfigured or only able to produce a presigned URL: refused with guidance to set a permanent public base.
- GraphQL error for a channel: becomes that channel's `error` string; other channels still post.
- Unparseable `when`: clear error before any post is created.
- `IRIS_PUBLISH_DIR` violation: refused (unchanged).
- Unexpected Buffer response shape: `BufferError`, surfaced as a failure, never an uncaught crash.

## Testing

Unit tests with injected HTTP and fake responses, mirroring the existing `social.py` tests, so the feature is built and tested with no Buffer account:

- `list_channels` response parsing.
- `resolve_channels`: all (empty names), named subset, unknown names.
- `create_post`: mutation/variables construction, success parsing, GraphQL-error parsing, now vs scheduled.
- `publish` orchestrator: per-channel fail-soft (one channel errors, others succeed), video hosted once and reused.
- `stable_media_host`: returns a permanent URL when a public base is configured; refuses (raises `BufferError`) when only presigned hosting is available.
- MCP tool: `IRIS_PUBLISH_DIR` restriction, missing-file handling, output formatting.

Live verification (the first real post to real channels) is a manual step once the account and a permanent media host exist, documented as the real test, mirroring the YouTube/Instagram note that "the first live post is the real test."

## Prerequisites (for live use, not for building)

- A Buffer account with channels connected and a personal API token in `IRIS_BUFFER_TOKEN`.
- A stable public media host: a Cloudflare R2 public bucket (or Cloudinary) configured so `stable_media_host` returns a permanent URL. Reuses the existing `IRIS_MEDIA_BUCKET` / `IRIS_MEDIA_ENDPOINT` / `IRIS_MEDIA_PUBLIC_BASE` env vars, with `IRIS_MEDIA_PUBLIC_BASE` now required.
- `docs/PUBLISHING-SETUP.md` rewritten for Buffer (Buffer token, channel connection, permanent media host).
- mcp.json keeps the publish server; allowlist keeps `mcp__publish__publish_video`.

## Out of scope (v1, YAGNI)

- Custom thumbnails (Buffer auto-derives; `thumbnailUrl` plumbing can come later).
- Per-channel caption variants (Buffer supports per-network metadata; v1 uses one caption for all).
- Adding posts to Buffer's own queue (we pick "now" or an explicit time).
- Reading analytics/metrics back from Buffer.

## Open item to confirm during implementation

- Exact GraphQL endpoint URL, auth header format, and the precise `createPost` input shape (channel field name, asset field names) pinned from developers.buffer.com at build time.
- Whether TikTok accepts video via the beta API (confirmed by live test; the fail-soft design degrades gracefully if not).
