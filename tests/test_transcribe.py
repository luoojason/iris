"""Tests for local voice transcription (no whisper dependency required).

The whisper model is never loaded here: every test injects a fake transcriber,
so these run anywhere and prove the wiring, not the model.
"""

from __future__ import annotations

from iris.transcribe import (
    build_transcriber,
    looks_like_audio,
    transcribe_audio,
)


def test_looks_like_audio_by_extension():
    assert looks_like_audio("/x/voice.ogg")
    assert looks_like_audio("/x/note.MP3")  # case-insensitive
    assert looks_like_audio("/x/clip.m4a")
    assert not looks_like_audio("/x/photo.png")
    assert not looks_like_audio("/x/report.pdf")
    assert not looks_like_audio("/x/noext")


def test_transcribe_audio_only_touches_audio_paths():
    seen = []

    def fake(path):
        seen.append(path)
        return "transcribed words"

    out = transcribe_audio(["/a/voice.ogg", "/a/pic.png"], fake)
    assert seen == ["/a/voice.ogg"]  # the image was never sent to the transcriber
    assert out == {"/a/voice.ogg": "transcribed words"}


def test_transcribe_audio_skips_when_no_transcriber():
    assert transcribe_audio(["/a/voice.ogg"], None) == {}


def test_transcribe_audio_drops_empty_results():
    out = transcribe_audio(["/a/voice.ogg"], lambda p: "   ")
    assert out == {}  # blank transcript -> fall back to a plain file ref


def test_transcribe_audio_survives_a_failing_clip():
    def boom(path):
        raise RuntimeError("decode failed")

    # One bad clip must not raise; it is simply omitted.
    assert transcribe_audio(["/a/voice.ogg"], boom) == {}


def test_build_transcriber_off_by_default():
    class Cfg:
        voice_enabled = False
        voice_model = "base"

    assert build_transcriber(Cfg()) is None


def test_build_transcriber_returns_whisper_when_enabled():
    class Cfg:
        voice_enabled = True
        voice_model = "small"

    t = build_transcriber(Cfg())
    assert t is not None
    assert t.model_size == "small"  # but the model itself is not loaded yet
    assert t._model is None
