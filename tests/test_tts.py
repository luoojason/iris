"""Tests for local text-to-speech (no real speech engine required)."""

from __future__ import annotations

import pytest

from iris import tts


def test_synthesize_with_explicit_command(tmp_path, monkeypatch):
    # A trivial "engine": copy stdin to the output file.
    monkeypatch.setenv("IRIS_TTS_CMD", "cat > {out}")
    out = tmp_path / "speech.wav"
    result = tts.synthesize("hello world", str(out))
    assert result == str(out)
    assert out.read_text() == "hello world"


def test_synthesize_empty_text_raises(tmp_path):
    with pytest.raises(tts.TTSUnavailable):
        tts.synthesize("   ", str(tmp_path / "x.wav"))


def test_synthesize_no_engine_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("IRIS_TTS_CMD", raising=False)
    monkeypatch.delenv("IRIS_TTS_VOICE", raising=False)
    monkeypatch.setattr(tts.shutil, "which", lambda name: None)  # nothing installed
    assert tts.tts_available() is False
    with pytest.raises(tts.TTSUnavailable):
        tts.synthesize("hi", str(tmp_path / "x.wav"))


def test_synthesize_reports_engine_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("IRIS_TTS_CMD", "exit 1")  # runs, fails, writes nothing
    with pytest.raises(tts.TTSUnavailable):
        tts.synthesize("hi", str(tmp_path / "x.wav"))


def test_tts_available_true_with_command(monkeypatch):
    monkeypatch.setenv("IRIS_TTS_CMD", "cat > {out}")
    assert tts.tts_available() is True
