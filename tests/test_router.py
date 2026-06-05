"""Tests for per-turn model routing (a pure decision, no model calls).

The router only ever downgrades: it returns the light model for a clearly
trivial turn, or None (meaning "use the driver's default strong model").
"""

from __future__ import annotations

from iris.router import choose_model

LIGHT = "claude-haiku-4-5"


def route(text, **kw):
    return choose_model(text, light_model=LIGHT, **kw)


def test_routing_off_without_light_model():
    assert choose_model("hey", light_model=None) is None


def test_trivial_message_goes_light():
    assert route("thanks!") == LIGHT
    assert route("lol that's great") == LIGHT
    assert route("good morning") == LIGHT


def test_long_message_stays_default():
    assert route("x" * 200) is None


def test_reasoning_keywords_stay_default():
    assert route("why does this happen") is None
    assert route("can you debug this") is None
    assert route("explain it") is None


def test_code_fence_stays_default():
    assert route("look:\n```\nprint(1)\n```") is None


def test_attachments_stay_default():
    assert route("nice", has_attachments=True) is None


def test_short_question_stays_light_but_long_question_defaults():
    assert route("you there?") == LIGHT
    assert route("what is the best way to structure this whole thing for me please?") is None
