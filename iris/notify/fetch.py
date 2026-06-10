"""Fetch a watch's current value (HTTP GET or shell command) and extract it.

Both functions are total: any network, command, parse, or timeout failure is
turned into a sentinel string starting with "<error:" rather than raised, so a
failure (a site going down, a broken command) registers as a value change and
surfaces, instead of crashing the tick or being silently swallowed.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.error
import urllib.request


def fetch(watch, *, http_get=None, runner=None) -> str:
    """Return the raw value for a watch. Injectable backends for tests."""
    try:
        if watch.get("url"):
            return _http(watch, http_get)
        if watch.get("cmd"):
            return _command(watch, runner)
        return "<error: watch has neither url nor cmd>"
    except Exception as exc:
        return f"<error: {exc}>"


def _http(watch, http_get) -> str:
    get = http_get or _default_http_get
    status, body = get(watch["url"])
    if watch["extract"]["kind"] == "status":
        return str(status)
    return body


def _default_http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "iris-watch/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return getattr(resp, "status", 200), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # A 4xx/5xx still has a status code, which is what a --status watch wants.
        return exc.code, exc.read().decode("utf-8", "replace")


def _command(watch, runner) -> str:
    if runner is not None:
        return runner(watch["cmd"])
    # shell=True is intentional: the whole point of the --cmd backend is to run a
    # shell command the OWNER wrote (e.g. "git ls-remote ... | head"). Watches are
    # added only via the owner's own CLI on their own single-user box, so there is
    # no untrusted-input path here. If a future MCP tool ever lets the agent create
    # watches from chat, this becomes an injection surface and must be revisited.
    proc = subprocess.run(watch["cmd"], shell=True, capture_output=True, text=True, timeout=60)
    return proc.stdout if proc.returncode == 0 else (proc.stdout + proc.stderr)


def extract(raw: str, watch) -> str:
    """Pull the watched value out of the raw fetch result."""
    if raw.startswith("<error:"):
        return raw  # keep a fetch error stable across ticks so it pings once
    kind = watch["extract"]["kind"]
    arg = watch["extract"]["arg"]
    try:
        if kind in ("text", "status"):
            return raw.strip()
        if kind == "json":
            obj = json.loads(raw)
            for part in [p for p in arg.split(".") if p]:
                obj = obj[int(part)] if isinstance(obj, list) else obj[part]
            return str(obj)
        if kind == "regex":
            match = re.search(arg, raw)
            return "" if not match else (match.group(1) if match.groups() else match.group(0))
        return raw.strip()
    except Exception as exc:
        return f"<error: {exc}>"
