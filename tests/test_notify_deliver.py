"""Tests for notify delivery (Discord sender injected)."""

from __future__ import annotations

from iris.notify import deliver


def test_no_target_returns_false():
    assert deliver.send("hi", token="", channel="123") is False
    assert deliver.send("hi", token="t", channel="") is False


def test_sends_via_injected_sender():
    calls = []

    def fake(channel, text, token):
        calls.append((channel, text, token))
        return True

    assert deliver.send("hello", token="t", channel="123", sender=fake) is True
    assert calls == [("123", "hello", "t")]


def test_sender_exception_is_false():
    def boom(channel, text, token):
        raise RuntimeError("network down")

    assert deliver.send("x", token="t", channel="123", sender=boom) is False
