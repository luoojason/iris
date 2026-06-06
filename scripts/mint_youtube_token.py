#!/usr/bin/env python3
"""Mint a YouTube refresh token for Iris, once, on a machine with a browser.

Run this AFTER you've set the OAuth app to "In production" and created a
"Desktop app" OAuth client (see docs/PUBLISHING-SETUP.md). It does the loopback
OAuth flow with access_type=offline + prompt=consent (which is what makes Google
return a refresh token) and prints the token, ready to paste into your
IRIS_SOCIAL_TOKENS file.

Usage:
  python scripts/mint_youtube_token.py <client_id> <client_secret>
  # or set YT_CLIENT_ID / YT_CLIENT_SECRET in the environment

Stdlib only, no pip install needed.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

SCOPE = "https://www.googleapis.com/auth/youtube.upload"
AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
PORT = 8765
REDIRECT = f"http://localhost:{PORT}/"


def main() -> int:
    cid = (sys.argv[1] if len(sys.argv) > 1 else "") or _env("YT_CLIENT_ID")
    secret = (sys.argv[2] if len(sys.argv) > 2 else "") or _env("YT_CLIENT_SECRET")
    if not cid or not secret:
        print("Need a client id and secret. See docs/PUBLISHING-SETUP.md.")
        return 1

    params = urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": REDIRECT,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
    })
    url = f"{AUTH}?{params}"
    print("Opening your browser to authorize. If it doesn't open, visit:\n", url, "\n")
    webbrowser.open(url)

    code_holder: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.urlparse(self.path).query
            code_holder["code"] = urllib.parse.parse_qs(q).get("code", [""])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Authorized. You can close this tab and return to the terminal.")

        def log_message(self, *a):  # silence the default logging
            pass

    server = HTTPServer(("localhost", PORT), Handler)
    server.handle_request()  # serve exactly one request (the redirect)
    code = code_holder.get("code")
    if not code:
        print("No authorization code received.")
        return 1

    data = urllib.parse.urlencode({
        "code": code,
        "client_id": cid,
        "client_secret": secret,
        "redirect_uri": REDIRECT,
        "grant_type": "authorization_code",
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(TOKEN, data=data)) as resp:
        body = json.loads(resp.read())

    refresh = body.get("refresh_token")
    if not refresh:
        print("No refresh token returned. Make sure the app is 'In production' and you used a fresh consent.\n", body)
        return 1

    print("\nSuccess. Add these to your IRIS_SOCIAL_TOKENS file:\n")
    print(json.dumps({
        "yt_client_id": cid,
        "yt_client_secret": secret,
        "yt_refresh_token": refresh,
    }, indent=2))
    return 0


def _env(name: str) -> str:
    import os

    return os.environ.get(name, "")


if __name__ == "__main__":
    raise SystemExit(main())
