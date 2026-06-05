"""Local text-to-speech, so the agent can reply out loud.

Like the inbound voice path, this stays free and local: it shells out to a
text-to-speech binary you already have rather than a paid API. It tries, in
order, an explicit command you configure, then piper, then macOS ``say``, then
``espeak-ng``/``espeak``. If none is present it raises ``TTSUnavailable`` and the
caller just skips the spoken reply.

Configure a specific engine with ``IRIS_TTS_CMD`` (a template that reads the text
on stdin and writes audio to the path in ``{out}``) or, for piper, point
``IRIS_TTS_VOICE`` at a voice ``.onnx`` model.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Optional


class TTSUnavailable(RuntimeError):
    """No usable text-to-speech engine was found or it failed."""


def _backend_command(out_path: str) -> Optional[list[str]]:
    """The argv for the first available engine, or None if none is installed.

    Each command reads the text on stdin (except where noted) and writes audio to
    ``out_path``. ``IRIS_TTS_CMD`` wins; it is a template using ``{out}``.
    """
    template = os.environ.get("IRIS_TTS_CMD")
    if template:
        return ["/bin/sh", "-c", template.format(out=out_path)]
    voice = os.environ.get("IRIS_TTS_VOICE")
    if voice and shutil.which("piper"):
        return ["piper", "--model", voice, "--output_file", out_path]
    if shutil.which("say"):  # macOS
        return ["say", "-o", out_path]
    for engine in ("espeak-ng", "espeak"):
        if shutil.which(engine):
            return [engine, "--stdin", "-w", out_path]
    return None


def tts_available() -> bool:
    """Whether any text-to-speech engine is configured or installed."""
    return _backend_command("/tmp/_probe") is not None


def synthesize(text: str, out_path: str, timeout: float = 60.0) -> str:
    """Render ``text`` to an audio file at ``out_path``. Returns the path.

    Raises ``TTSUnavailable`` if no engine is present or synthesis fails.
    """
    text = (text or "").strip()
    if not text:
        raise TTSUnavailable("nothing to speak")
    cmd = _backend_command(out_path)
    if cmd is None:
        raise TTSUnavailable(
            "no TTS engine found; install piper, espeak-ng, or set IRIS_TTS_CMD"
        )
    try:
        proc = subprocess.run(cmd, input=text, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TTSUnavailable(f"TTS engine failed to run: {exc}") from exc
    if proc.returncode != 0 or not os.path.exists(out_path):
        raise TTSUnavailable(f"TTS engine error: {(proc.stderr or '').strip()[:200]}")
    return out_path
