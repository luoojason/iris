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
