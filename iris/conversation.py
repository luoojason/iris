"""Per-conversation turn runner: let the user keep talking while the agent works.

A one-shot request/response loop has two gaps a chat user feels immediately. The
first: a slow task gives no sign of life until it finishes, so a long turn looks
like a hang. The second: a message sent while a turn is in flight either blocks
silently or, worse, races another turn on the same ``--resume`` session. This
module closes both, transport-agnostically. The Discord/Telegram adapters supply
only the I/O (send a message, show a typing indicator, acknowledge receipt of a
message); the orchestration lives here, once, and is unit-testable without any
chat SDK.

One :class:`ConversationRunner` owns one conversation. It serializes that
conversation's turns (never two ``claude --resume`` at once) and *coalesces* any
messages that pile up while a turn runs into the next turn, so by the following
turn the agent's view of the conversation is fully current. A turn that runs
past ``ack_delay`` fires a short interim line ("on it") so the user is never left
guessing, and that line is skipped entirely for fast turns so trivial messages
are not spammed with one.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, List, Optional, Protocol, Sequence

log = logging.getLogger("iris.conversation")


def coalesce_messages(messages: Sequence[str]) -> str:
    """Fold consecutive user messages into one prompt.

    A single message is returned unchanged so the common case is untouched.
    Several messages (the user kept typing while a turn ran) are joined into one
    prompt, lightly framed so the agent reads them as the burst they were rather
    than one run-on sentence. Empty/whitespace messages are dropped.
    """
    cleaned = [m.strip() for m in messages if m and m.strip()]
    if not cleaned:
        return ""
    if len(cleaned) == 1:
        return cleaned[0]
    body = "\n\n".join(f"{i}. {m}" for i, m in enumerate(cleaned, 1))
    return (
        "I sent these messages one after another while you were working; "
        "treat them together as my latest turn:\n\n" + body
    )


@dataclass
class Turn:
    """One inbound message, prepared by the adapter and ready to run."""

    text: str
    has_attachments: bool = False
    # Called once if this message arrives while a turn is already running, to
    # confirm it was seen (e.g. add a reaction). Never called for a message that
    # itself starts a turn.
    receipt: Optional[Callable[[], Awaitable[None]]] = None


# Runs a coalesced prompt and returns the text to send back, or None to stay
# silent. The adapter implements this (wrapping ``agent.respond`` in a thread and
# turning errors into a user-facing string).
RunTurn = Callable[[str, bool], Awaitable[Optional[str]]]
Send = Callable[[str], Awaitable[None]]
Typing = Callable[[], "object"]  # () -> async context manager, or None


class ConversationRunner:
    """Serialize and coalesce one conversation's turns, with an interim ack."""

    def __init__(
        self,
        *,
        run_turn: RunTurn,
        send: Send,
        ack_line: Callable[[], Optional[str]],
        typing: Optional[Typing] = None,
        ack_delay: float = 4.0,
    ) -> None:
        self._run_turn = run_turn
        self._send = send
        self._ack_line = ack_line
        self._typing = typing
        self._ack_delay = ack_delay
        self._pending: list[Turn] = []
        self._worker: Optional[asyncio.Task] = None

    @property
    def busy(self) -> bool:
        return self._worker is not None and not self._worker.done()

    def submit(self, turn: Turn) -> None:
        """Queue a message. Starts a worker if idle; else confirms receipt.

        Cheap and non-blocking: the actual turn runs in a background task so the
        caller's event loop is free to receive the next message.
        """
        already_busy = self.busy
        self._pending.append(turn)
        if already_busy:
            if turn.receipt is not None:
                asyncio.ensure_future(self._safe_receipt(turn.receipt))
            return
        self._worker = asyncio.ensure_future(self._drain())

    async def _drain(self) -> None:
        try:
            while self._pending:
                batch, self._pending = self._pending, []
                prompt = coalesce_messages([t.text for t in batch])
                if not prompt:
                    continue
                has_attachments = any(t.has_attachments for t in batch)
                await self._run_one(prompt, has_attachments)
        finally:
            self._worker = None
            # A message could have been queued after the loop saw _pending empty
            # but before this clears _worker; restart so it is never stranded.
            if self._pending:
                self._worker = asyncio.ensure_future(self._drain())

    async def _run_one(self, prompt: str, has_attachments: bool) -> None:
        ack_task = asyncio.ensure_future(self._delayed_ack())
        try:
            cm = self._typing() if self._typing is not None else None
            if cm is not None:
                async with cm:
                    text = await self._run_turn(prompt, has_attachments)
            else:
                text = await self._run_turn(prompt, has_attachments)
        finally:
            ack_task.cancel()
        if text:
            await self._send(text)

    async def _delayed_ack(self) -> None:
        try:
            await asyncio.sleep(self._ack_delay)
        except asyncio.CancelledError:
            return  # turn finished first; no ack needed
        line = self._ack_line() if self._ack_line else None
        if not line:
            return
        try:
            await self._send(line)
        except Exception:  # an ack must never sink a turn
            log.warning("could not send interim ack", exc_info=True)

    async def _safe_receipt(self, receipt: Callable[[], Awaitable[None]]) -> None:
        try:
            await receipt()
        except Exception:
            log.debug("receipt failed", exc_info=True)


class LiveHandle(Protocol):
    """One streaming turn the runner drives. Implemented by the adapter.

    Mirrors :class:`iris.agent.LiveTurn` but at the text level: ``result`` and
    ``aftermath`` return user-facing strings (errors already mapped), so the
    runner stays free of any driver or model detail.
    """

    async def begin(self) -> None: ...
    def is_open(self) -> bool: ...
    async def inject(self, text: str) -> bool: ...
    async def result(self) -> Optional[str]: ...
    async def aftermath(self) -> List[str]: ...
    def close(self) -> None: ...


# Builds (but does not begin) a live turn for a coalesced prompt.
StartLiveTurn = Callable[[str, bool], LiveHandle]


class LiveConversationRunner:
    """Like :class:`ConversationRunner`, but a message that arrives mid-turn is
    injected into the running turn (a live redirect) instead of only folding into
    the next one. A message that lands exactly as the turn closes is refused by the
    turn and falls through to start the next turn, so nothing is lost.

    Turns are still serialized per conversation: only one live turn runs at a time,
    and messages that arrive while none is open are coalesced into the next turn,
    so the floor's guarantees still hold when no turn is in flight.
    """

    def __init__(
        self,
        *,
        start_turn: StartLiveTurn,
        send: Send,
        ack_line: Callable[[], Optional[str]],
        typing: Optional[Typing] = None,
        ack_delay: float = 4.0,
    ) -> None:
        self._start_turn = start_turn
        self._send = send
        self._ack_line = ack_line
        self._typing = typing
        self._ack_delay = ack_delay
        self._pending: list[Turn] = []
        self._live: Optional[LiveHandle] = None
        self._worker: Optional[asyncio.Task] = None

    @property
    def busy(self) -> bool:
        return self._worker is not None and not self._worker.done()

    def submit(self, turn: Turn) -> None:
        """Inject into the open turn if there is one; else queue for the next."""
        live = self._live
        if live is not None and live.is_open():
            asyncio.ensure_future(self._inject(turn))
            return
        self._pending.append(turn)
        if not self.busy:
            self._worker = asyncio.ensure_future(self._drain())

    async def _inject(self, turn: Turn) -> None:
        live = self._live
        accepted = False
        if live is not None:
            try:
                accepted = await live.inject(turn.text)
            except Exception:
                log.warning("could not inject mid-turn message", exc_info=True)
                accepted = False
        if accepted:
            if turn.receipt is not None:
                await self._safe_receipt(turn.receipt)
            return
        # The turn closed between the open-check and the write (the boundary race):
        # treat this message as the start of the next turn.
        self._pending.append(turn)
        if not self.busy:
            self._worker = asyncio.ensure_future(self._drain())

    async def _drain(self) -> None:
        try:
            while self._pending:
                batch, self._pending = self._pending, []
                prompt = coalesce_messages([t.text for t in batch])
                if not prompt:
                    continue
                has_attachments = any(t.has_attachments for t in batch)
                await self._run_live(prompt, has_attachments)
        finally:
            self._worker = None
            if self._pending:
                self._worker = asyncio.ensure_future(self._drain())

    async def _run_live(self, prompt: str, has_attachments: bool) -> None:
        handle = self._start_turn(prompt, has_attachments)
        try:
            await handle.begin()
        except Exception:
            log.error("could not start live turn", exc_info=True)
            handle.close()
            await self._send("Something went wrong starting that one. Try again in a moment.")
            return
        self._live = handle
        # close() releases the per-conversation lock and is idempotent. It MUST run
        # on every exit path: a send failure or a non-ClaudeError from result()
        # (e.g. a session-store IO error) would otherwise leak the lock and wedge
        # the conversation forever, since begin() acquired it for the whole turn.
        try:
            ack_task = asyncio.ensure_future(self._delayed_ack())
            try:
                cm = self._typing() if self._typing is not None else None
                if cm is not None:
                    async with cm:
                        reply = await handle.result()
                else:
                    reply = await handle.result()
            finally:
                ack_task.cancel()
            if reply:
                await self._send(reply)
            # aftermath waits the process out and surfaces any stray follow-up
            # (a message that raced the close boundary).
            strays = await handle.aftermath()
            for stray in strays:
                if stray:
                    await self._send(stray)
        finally:
            handle.close()
            self._live = None

    async def _delayed_ack(self) -> None:
        try:
            await asyncio.sleep(self._ack_delay)
        except asyncio.CancelledError:
            return
        line = self._ack_line() if self._ack_line else None
        if not line:
            return
        try:
            await self._send(line)
        except Exception:
            log.warning("could not send interim ack", exc_info=True)

    async def _safe_receipt(self, receipt: Callable[[], Awaitable[None]]) -> None:
        try:
            await receipt()
        except Exception:
            log.debug("receipt failed", exc_info=True)
