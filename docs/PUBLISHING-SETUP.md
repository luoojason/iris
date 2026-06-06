# Publishing setup (YouTube + Instagram)

Iris can publish a finished video to your own YouTube (as a Short) and Instagram
(as a Reel) through the platforms' official APIs. No third party, no scraping.
TikTok is intentionally not wired yet (its API gates public posting behind an app
audit that needs a per-post consent UI a headless agent doesn't have).

You do the account setup once (browser steps on your own accounts); Iris does the
posting after. The agent code is `iris/social.py` + the `publish_video` MCP tool.

When done, the tokens live in one JSON file on the box (`IRIS_SOCIAL_TOKENS`,
perms 600, never in the repo):

```json
{
  "yt_client_id": "...", "yt_client_secret": "...", "yt_refresh_token": "...",
  "ig_user_id": "...", "ig_access_token": "..."
}
```

## YouTube (Shorts)

1. **Google Cloud Console** → create a project → **enable "YouTube Data API v3"**.
2. **OAuth consent screen:** User type **External**. Add scope
   `https://www.googleapis.com/auth/youtube.upload`. Add yourself as a test user.
   Then **set Publishing status to "In production."** (Leave the app *unverified*,
   that's fine for personal use.)
   - Why this matters: in "Testing" mode Google revokes the refresh token after
     **7 days**, which would silently break the bot every week.
3. **IMPORTANT ORDER:** after going to "In production", **create the OAuth
   credentials** (Credentials → Create → OAuth client ID → **Desktop app**).
   Creating them *after* going to production is what avoids the 7-day expiry.
   Download the client ID + secret.
4. **Mint the refresh token once**, on a laptop with a browser, using
   `access_type=offline` and `prompt=consent` (the `prompt=consent` is what forces
   Google to actually return a refresh token). Any OAuth playground or a tiny
   local script works; I can give you a 10-line one. Copy `yt_client_id`,
   `yt_client_secret`, `yt_refresh_token` into the tokens file.
5. **Submit the YouTube API audit form**
   (`support.google.com/youtube/contact/yt_api_form`) on day one. Until it's
   approved, uploads land **private** and cannot be made public (by API or in
   Studio). Uploading still works immediately for testing; public just waits on
   this. Approval is at Google's discretion but routine for a small channel.

## Instagram (Reels)

1. In the Instagram app, switch your account to **Professional (Business or
   Creator)** — free, instant. (Personal accounts cannot use the API at all.)
2. At **developers.facebook.com**, create an app (free). Add the **Instagram**
   product and configure **"Instagram API with Instagram Login"** — NOT the
   Facebook Login variant (that one needs a linked Facebook Page; this one does
   not).
3. Add yourself as an **app role** (admin/developer/tester). This keeps you on
   **Standard Access**, which needs **no App Review** to publish to your own
   account.
4. Request scopes `instagram_business_basic` and
   `instagram_business_content_publish`.
5. Run the **Business Login** OAuth flow once in a browser → exchange for a
   **long-lived token (60 days)**. Put it and your IG user id into the tokens
   file as `ig_access_token` and `ig_user_id`.
   - The 60-day token must be **refreshed every ~30–45 days** or it expires
     permanently and you re-auth in a browser. Iris can run that refresh on a
     timer once it's live.
6. **Hosting:** Meta fetches the video from a public HTTPS URL (you hand it a
   link, not the file). Configure an S3-compatible bucket — a **Cloudflare R2**
   or **Backblaze B2** free tier works — via the env vars below. Iris uploads the
   mp4 there, hands Instagram a presigned URL, and the file can be deleted after.

## Environment (on the box)

```bash
IRIS_SOCIAL_TOKENS=/home/irisbot/iris-social.json   # the 600 tokens file above

# Instagram media hosting (S3-compatible: R2 / B2 / S3)
IRIS_MEDIA_BUCKET=your-bucket
IRIS_MEDIA_ENDPOINT=https://<accountid>.r2.cloudflarestorage.com   # omit for AWS S3
IRIS_MEDIA_PUBLIC_BASE=                                            # set if the bucket is public; else presigned URLs are used
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
```

Then add the publish server to the bot's `mcp.json` and allowlist
`mcp__publish__publish_video`. The agent calls it as
`publish_video(mp4_path, caption, platforms="youtube,instagram", privacy="public")`.

## Status

The publishing code is unit-tested but **not yet verified against live accounts**
(that needs the setup above). YouTube uploads will land private until the audit
clears; Instagram is public immediately once the token + hosting are configured.
