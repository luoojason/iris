"""Test the Textual TUI with a fake agent. Skipped where textual is absent."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from iris.driver import ClaudeResult
from iris.tui import build_app


class FakeAgent:
    def __init__(self):
        self.calls = []
        self.reset_calls = []

    def respond(self, conversation_id, text):
        self.calls.append((conversation_id, text))
        return ClaudeResult(text=f"echo: {text}", session_id="s1", is_error=False)

    def reset(self, conversation_id):
        self.reset_calls.append(conversation_id)
        return True


async def test_submitting_a_message_calls_the_agent():
    agent = FakeAgent()
    app = build_app(agent)()
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "hello there"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert agent.calls == [("tui:local", "hello there")]


async def test_reset_command_resets_the_conversation():
    agent = FakeAgent()
    app = build_app(agent)()
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/reset"
        await pilot.press("enter")
        await pilot.pause()
    assert agent.reset_calls == ["tui:local"]
    assert agent.calls == []  # /reset is not sent to the model


async def test_blank_input_does_nothing():
    agent = FakeAgent()
    app = build_app(agent)()
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "   "
        await pilot.press("enter")
        await pilot.pause()
    assert agent.calls == []
