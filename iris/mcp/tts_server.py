"""MCP server: speak a reply out loud on Discord.

Gives the agent a ``speak`` tool: it renders text to speech with a local engine
(see ``iris/tts.py``) and uploads the audio to a Discord channel, defaulting to
the home channel. Free and local, like the inbound voice transcription; there is
no paid voice API. Allowlist ``mcp__tts__speak`` and tell the persona it can
speak when a voice reply fits.
"""

from __future__ import annotations

import os
import tempfile
import urllib.error
import urllib.request
import uuid

from ..tts import TTSUnavailable, synthesize

try:
    from mcp.server.fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Needs the MCP SDK: pip install 'iris-agent[memory]'") from exc

API = "https://discord.com/api/v10"
mcp = FastMCP("iris-tts")


def _token() -> str:
    return os.environ.get("IRIS_DISCORD_TOKEN") or os.environ.get("DISCORD_BOT_TOKEN", "")


def _home_channel(given: str) -> str:
    return given or os.environ.get("IRIS_DISCORD_HOME_CHANNEL", "")


def _multipart(content: str, filename: str, audio: bytes) -> tuple[str, bytes]:
    """Encode a Discord message-with-file body. Returns (content_type, body)."""
    boundary = f"----iris{uuid.uuid4().hex}"
    payload = (
        '{"content": %s, "attachments": [{"id": 0, "filename": %s}]}'
        % (_json_str(content), _json_str(filename))
    )
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
        f"Content-Type: application/json\r\n\r\n{payload}\r\n".encode(),
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="files[0]"; '
            f'filename="{filename}"\r\nContent-Type: audio/wav\r\n\r\n'
        ).encode()
        + audio
        + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


def _json_str(value: str) -> str:
    import json

    return json.dumps(value)


def _post_audio(channel_id: str, file_path: str, content: str = "") -> dict:
    """Upload an audio file as a message to a channel. The testable network seam."""
    with open(file_path, "rb") as handle:
        audio = handle.read()
    content_type, body = _multipart(content, "speech.wav", audio)
    req = urllib.request.Request(
        f"{API}/channels/{channel_id}/messages",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bot {_token()}",
            "Content-Type": content_type,
            "User-Agent": "iris (https://github.com/luoojason/iris, 0.1)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"ok": True, "status": resp.status}
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}", "detail": exc.read().decode("utf-8", "replace")[:200]}
    except Exception as exc:  # network, timeout
        return {"error": str(exc)}


@mcp.tool()
def speak(text: str, channel_id: str = "") -> str:
    """Speak a short reply out loud in a Discord channel (defaults to home).

    Renders the text to speech locally and posts the audio. Use it for replies
    that are nicer heard than read; keep them short.

    Args:
        text: What to say.
        channel_id: Channel to post to; defaults to the home channel.
    """
    channel = _home_channel(channel_id)
    if not channel:
        return "No channel id given and no home channel configured."
    out_path = os.path.join(tempfile.gettempdir(), f"iris-speech-{uuid.uuid4().hex}.wav")
    try:
        try:
            synthesize(text, out_path)
        except TTSUnavailable as exc:
            return f"Could not speak: {exc}"
        res = _post_audio(channel, out_path, content="")
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass
    if isinstance(res, dict) and "error" in res:
        return f"Synthesized but could not post: {res['error']} {res.get('detail', '')}".strip()
    return "Spoke the reply."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
