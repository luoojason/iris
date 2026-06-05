"""Local speech-to-text for inbound voice messages.

The brain (the ``claude`` binary) reads text and images, not audio. So when a
voice message arrives we transcribe it to text *in the adapter*, before the
prompt reaches the brain, and fold the transcript into the prompt the same way
an image's path is folded in. This is the right seam: an MCP tool the model
calls could not cleanly intercept a Discord voice attachment.

Transcription is local and free (``faster-whisper``), so it keeps the project's
zero-extra-cost shape. It is also optional and lazy: the model is only imported
and loaded the first time a voice message actually arrives, which preserves the
zero-idle-inference promise. With no transcriber configured, audio attachments
degrade gracefully to plain file references.
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional

log = logging.getLogger("iris.transcribe")

# Audio containers Discord and Telegram produce for voice notes and uploads.
AUDIO_EXTS = {
    ".ogg", ".oga", ".opus", ".mp3", ".m4a", ".mp4a",
    ".wav", ".webm", ".flac", ".aac", ".amr",
}

# A transcriber turns an audio file path into its transcript text.
Transcriber = Callable[[str], str]


def looks_like_audio(path: str) -> bool:
    """True when a saved attachment is an audio file we could transcribe."""
    _, ext = os.path.splitext(path.lower())
    return ext in AUDIO_EXTS


class WhisperTranscriber:
    """A ``faster-whisper`` backed transcriber, loaded lazily on first use.

    ``faster-whisper`` runs on the CPU via CTranslate2, so there is no PyTorch
    dependency and a small ``int8`` model is enough for short voice notes. The
    model file (~tens of MB for ``base``) downloads on first call and is cached.
    """

    def __init__(self, model_size: str = "base"):
        self.model_size = model_size
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            from faster_whisper import WhisperModel  # lazy: only when voice is used

            log.info("loading whisper model %r (first voice message)", self.model_size)
            self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
        return self._model

    def __call__(self, path: str) -> str:
        model = self._ensure_model()
        # transcribe() returns a generator of segments; consume it fully.
        segments, _info = model.transcribe(path)
        return " ".join(segment.text for segment in segments).strip()


def build_transcriber(config) -> Optional[Transcriber]:
    """Build a transcriber from config, or None when voice is disabled.

    Returns None (rather than raising) so a misconfigured or dependency-less
    install simply skips transcription instead of crashing the bot.
    """
    if not getattr(config, "voice_enabled", False):
        return None
    return WhisperTranscriber(getattr(config, "voice_model", "base") or "base")


def transcribe_audio(paths, transcriber: Optional[Transcriber]) -> dict[str, str]:
    """Transcribe the audio attachments in ``paths``.

    Returns ``{path: transcript}`` for the audio files we could transcribe. A
    file that fails (or comes back empty) is left out, so the caller falls back
    to passing it as a plain attachment path. Blocking work — call from a thread.
    """
    transcripts: dict[str, str] = {}
    if not transcriber:
        return transcripts
    for path in paths:
        if not looks_like_audio(path):
            continue
        try:
            text = transcriber(path)
        except Exception as exc:  # a bad clip must not sink the whole turn
            log.warning("could not transcribe %s: %s", path, exc)
            continue
        if text and text.strip():
            transcripts[path] = text.strip()
    return transcripts
