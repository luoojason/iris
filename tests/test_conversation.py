"""Tests for the per-conversation runner: coalescing, queueing, interim ack.

These exercise the transport-agnostic orchestration with fake I/O, so no chat
SDK is needed. Timing-sensitive cases use a tiny ack_delay and an Event to hold a
turn open deterministically rather than sleeping for real.
"""

from __future__ import annotations

import asyncio

import pytest

from iris.conversation import ConversationRunner, Turn, coalesce_messages


def test_coalesce_single_is_unchanged():
    assert coalesce_messages(["hello there"]) == "hello there"


def test_coalesce_drops_blank_and_returns_lone_survivor():
    assert coalesce_messages(["", "  ", "only one"]) == "only one"


def test_coalesce_empty_is_empty():
    assert coalesce_messages([]) == ""
    assert coalesce_messages(["", "   "]) == ""


def test_coalesce_multiple_are_framed_and_numbered():
    out = coalesce_messages(["do X", "also Y"])
    assert "do X" in out and "also Y" in out
    assert "1. do X" in out and "2. also Y" in out


def _runner(**kw):
    """Build a runner with capture lists; returns (runner, sent, turns)."""
    sent: list[str] = []
    turns: list[tuple[str, bool]] = []

    async def send(text):
        sent.append(text)

    async def run_turn(prompt, has_attachments):
        turns.append((prompt, has_attachments))
        return f"reply to: {prompt}"

    runner = ConversationRunner(
        run_turn=kw.get("run_turn", run_turn),
        send=send,
        ack_line=kw.get("ack_line", lambda: "on it"),
        ack_delay=kw.get("ack_delay", 0.05),
    )
    return runner, sent, turns


def test_single_message_runs_and_replies():
    async def go():
        runner, sent, turns = _runner(ack_delay=10)  # ack never fires
        runner.submit(Turn(text="hi"))
        await runner._worker
        assert turns == [("hi", False)]
        assert sent == ["reply to: hi"]

    asyncio.run(go())


def test_messages_during_a_turn_coalesce_into_the_next():
    async def go():
        gate = asyncio.Event()
        seen: list[str] = []

        async def run_turn(prompt, has_attachments):
            seen.append(prompt)
            if len(seen) == 1:
                await gate.wait()  # hold the first turn open
            return f"done: {prompt}"

        sent: list[str] = []

        async def send(text):
            sent.append(text)

        receipts: list[str] = []

        async def receipt():
            receipts.append("seen")

        runner = ConversationRunner(
            run_turn=run_turn, send=send, ack_line=lambda: None, ack_delay=10
        )
        runner.submit(Turn(text="first"))
        await asyncio.sleep(0)  # let the worker start the first turn
        # Two messages arrive mid-turn: both should be acknowledged and folded.
        runner.submit(Turn(text="second", receipt=receipt))
        runner.submit(Turn(text="third", receipt=receipt))
        await asyncio.sleep(0)
        gate.set()
        await runner._worker

        assert seen[0] == "first"
        # second + third coalesced into one follow-up turn, in order
        assert "second" in seen[1] and "third" in seen[1]
        assert seen[1].index("second") < seen[1].index("third")
        assert len(seen) == 2
        assert len(receipts) == 2  # each interjection got a receipt

    asyncio.run(go())


def test_interim_ack_fires_only_when_a_turn_runs_long():
    async def go():
        gate = asyncio.Event()

        async def run_turn(prompt, has_attachments):
            await gate.wait()
            return "answer"

        sent: list[str] = []

        async def send(text):
            sent.append(text)

        runner = ConversationRunner(
            run_turn=run_turn, send=send, ack_line=lambda: "on it", ack_delay=0.02
        )
        runner.submit(Turn(text="slow one"))
        await asyncio.sleep(0.06)  # past ack_delay, turn still held open
        assert sent == ["on it"]  # interim ack landed before the answer
        gate.set()
        await runner._worker
        assert sent == ["on it", "answer"]

    asyncio.run(go())


def test_fast_turn_sends_no_interim_ack():
    async def go():
        runner, sent, _ = _runner(ack_delay=10)  # turn finishes well before ack
        runner.submit(Turn(text="quick"))
        await runner._worker
        assert sent == ["reply to: quick"]  # no "on it"

    asyncio.run(go())


def test_silent_turn_sends_nothing():
    async def go():
        async def run_turn(prompt, has_attachments):
            return None

        sent: list[str] = []

        async def send(text):
            sent.append(text)

        runner = ConversationRunner(
            run_turn=run_turn, send=send, ack_line=lambda: None, ack_delay=10
        )
        runner.submit(Turn(text="ignored"))
        await runner._worker
        assert sent == []

    asyncio.run(go())


def test_live_runner_closes_handle_when_send_fails():
    """Regression: a send failure mid-turn must still close the live handle, so the
    per-conversation lock is released and the conversation is not wedged forever."""
    from iris.conversation import LiveConversationRunner

    async def go():
        closed: list[bool] = []

        class FakeHandle:
            async def begin(self):
                return None

            def is_open(self):
                return False

            async def inject(self, text):
                return False

            async def result(self):
                return "the reply"

            async def aftermath(self):
                return []

            def close(self):
                closed.append(True)

        async def send(text):
            raise RuntimeError("discord send failed")

        runner = LiveConversationRunner(
            start_turn=lambda prompt, has_attachments: FakeHandle(),
            send=send,
            ack_line=lambda: None,
            ack_delay=10,  # the interim ack never fires in this test
        )
        runner.submit(Turn(text="hi"))
        worker = runner._worker
        assert worker is not None
        try:
            await worker
        except RuntimeError:
            pass  # the send error propagates out of the worker; the point is close() still ran
        assert closed == [True]

    asyncio.run(go())
