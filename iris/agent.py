"""Transport-agnostic agent core: turn a message into a reply.

Every front end (Discord, Telegram, the terminal) does the same three things on
each message: look up this conversation's session, run the brain, remember the
new session id. That lives here, once, so adding a transport is just wiring.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

from .config import Config
from .driver import ClaudeDriver, ClaudeResult
from .metrics import emit_turn
from .router import choose_model_explained
from .sessions import SessionStore
from .stream_driver import StreamDriver, StreamTurn

log = logging.getLogger("iris.agent")

# Asked of a session right before we retire it, to carry its memory forward.
COMPACT_PROMPT = (
    "Summarize our entire conversation so far so it can continue seamlessly in a "
    "fresh session. Capture the important facts, decisions, preferences, open "
    "threads, and any task state. Be thorough but concise. Reply with only the "
    "summary, no preamble."
)

# Seeds the fresh session with that summary so the next reply keeps context.
SEED_TEMPLATE = (
    "This conversation continues an earlier one. Here is a summary of everything "
    "so far, for your context:\n\n{summary}\n\nContinue naturally from here. Do "
    "not mention this summary or that the conversation was condensed."
)


def _is_dead_session(result: ClaudeResult) -> bool:
    """True when an error means the resumed claude session no longer exists."""
    blob = (result.error or "").lower()
    return "no conversation found" in blob or ("session" in blob and "not found" in blob)


def _is_overflow(result: ClaudeResult) -> bool:
    """True when an error means the session outgrew the model's context window."""
    blob = (result.error or "").lower()
    return any(
        marker in blob
        for marker in (
            "prompt is too long",
            "too long",
            "context_length",
            "context length",
            "maximum context",
            "exceeds the maximum",
        )
    )


class Agent:
    def __init__(
        self,
        driver: ClaudeDriver,
        store: SessionStore,
        compact_every: int = 0,
        compact_at_tokens: int = 0,
        light_model: str = "",
        metrics_file: str = "",
        trivial_max_chars: int = 140,
        stream_driver: Optional[StreamDriver] = None,
    ):
        self.driver = driver
        self.store = store
        # Built only when live interrupt is enabled; the one-shot driver is always
        # present and remains the fallback path.
        self.stream_driver = stream_driver
        # When set, trivial turns are routed to this lighter model to save credit;
        # everything else uses the driver's default (strong) model.
        self.light_model = light_model
        self.trivial_max_chars = trivial_max_chars
        self.metrics_file = metrics_file
        # Compact a conversation after this many turns on one session (0 = never).
        # A coarse backstop; the token threshold below is the accurate trigger.
        self.compact_every = compact_every
        # Compact once a turn's context reaches this many tokens (0 = never). This
        # catches tool-heavy turns (a big web fetch) that turn-count would miss.
        self.compact_at_tokens = compact_at_tokens
        # When False, compaction runs inline instead of in a background thread
        # (used by tests for determinism).
        self.compact_async = True
        # After a failed compaction, wait this long before trying again so a
        # doomed summary does not re-launch on every turn, blocking and billing
        # until the overflow heal eventually fires. Injectable clock for tests.
        self.compact_failure_cooldown = 600.0
        self._clock = time.monotonic
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._compacting: set[str] = set()
        self._compact_cooldown_until: dict[str, float] = {}
        # Bumped on reset(); a live turn captures it at start and refuses to write
        # a stale session back if a reset advanced it mid-turn.
        self._epochs: dict[str, int] = {}
        self._last_compaction: threading.Thread | None = None

    def _lock_for(self, conversation_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(conversation_id, threading.Lock())

    def respond(
        self,
        conversation_id: str,
        text: str,
        has_attachments: bool = False,
        model: str | None = None,
    ) -> ClaudeResult:
        """Run one turn for a conversation and persist its session id.

        Turns for the same conversation are serialized so two messages arriving
        at once do not run concurrent --resume turns and fork the transcript.

        ``model`` forces a specific model for this turn, bypassing the heuristic
        router (a caller can pin the strong model for a known-hard message or the
        light one for a known-cheap batch). When ``None`` the router decides.
        """
        if model is not None:
            reason, routed = "forced", "forced"
        else:
            model, reason = choose_model_explained(
                text,
                light_model=self.light_model or None,
                has_attachments=has_attachments,
                trivial_max_chars=self.trivial_max_chars,
            )
            routed = "light" if model else ("default" if reason == "light-disabled" else "strong")
        with self._lock_for(conversation_id):
            session_id = self.store.get(conversation_id)
            result = self.driver.run(text, session_id, model)
            # A resumed session that no longer exists, or one that outgrew the
            # context window, carries no replacement id. Either way the stored id
            # is unusable, so drop it and retry once on a fresh session.
            if result.is_error and session_id and (_is_dead_session(result) or _is_overflow(result)):
                if _is_overflow(result):
                    log.warning("conversation %s overflowed its context; starting fresh", conversation_id)
                self.store.clear(conversation_id)
                result = self.driver.run(text, None, model)
            if result.session_id:
                self.store.set(conversation_id, result.session_id)
            emit_turn(
                self.metrics_file, conversation_id, result, routed, reason,
                has_attachments, self.store.turns(conversation_id),
            )
            due_to_compact = not result.is_error and self._should_compact(conversation_id, result)
        # Outside the lock: the user already has their reply. Compaction runs on
        # its own and re-acquires the lock, so it never delays this turn.
        if due_to_compact:
            self._launch_compaction(conversation_id)
        return result

    def live_turn(self, conversation_id: str, text: str, has_attachments: bool = False) -> "LiveTurn":
        """Begin a turn the user can redirect mid-flight (live interrupt).

        Returns a :class:`LiveTurn` the caller drives: it injects follow-ups into
        the running turn, awaits the reply, and on completion persists the session
        and triggers compaction, exactly like :meth:`respond` but spread across the
        life of a streaming turn. The model is routed from the opening message,
        since the process is launched before any redirect can arrive.
        """
        if self.stream_driver is None:
            raise RuntimeError("live_turn requires a stream_driver (live interrupt is off)")
        model, reason = choose_model_explained(
            text,
            light_model=self.light_model or None,
            has_attachments=has_attachments,
            trivial_max_chars=self.trivial_max_chars,
        )
        routed = "light" if model else ("default" if reason == "light-disabled" else "strong")
        return LiveTurn(self, conversation_id, text, model,
                        routed=routed, reason=reason, has_attachments=has_attachments)

    def _should_compact(self, conversation_id: str, result: ClaudeResult) -> bool:
        """Whether this conversation is big enough to condense onto a fresh session."""
        cooldown_until = self._compact_cooldown_until.get(conversation_id)
        if cooldown_until is not None and self._clock() < cooldown_until:
            return False  # a recent compaction failed; do not hammer it every turn
        if self.compact_at_tokens > 0 and (result.context_tokens or 0) >= self.compact_at_tokens:
            return True
        if self.compact_every > 0 and self.store.turns(conversation_id) >= self.compact_every:
            return True
        return False

    def _note_compaction_failure(self, conversation_id: str) -> None:
        self._compact_cooldown_until[conversation_id] = self._clock() + self.compact_failure_cooldown

    def _launch_compaction(self, conversation_id: str) -> None:
        if not self.compact_async:
            self.compact(conversation_id)
            return
        with self._locks_guard:
            if conversation_id in self._compacting:
                return
            self._compacting.add(conversation_id)

        def _run() -> None:
            try:
                self.compact(conversation_id)
            finally:
                with self._locks_guard:
                    self._compacting.discard(conversation_id)

        thread = threading.Thread(target=_run, name=f"compact:{conversation_id}", daemon=True)
        self._last_compaction = thread
        thread.start()

    def compact(self, conversation_id: str) -> bool:
        """Condense a long conversation into a summary on a fresh session.

        Done while the current session is still valid (before it overflows), so
        the summary call itself is safe. Returns True if a new session replaced
        the old one.
        """
        lock = self._lock_for(conversation_id)
        with lock:
            session_id = self.store.get(conversation_id)
            if not session_id:
                return False
            # The summary resumes the live session, so it runs under the lock to
            # avoid forking it. This is the only model call that blocks the next
            # user turn (the seed below does not, since it touches no live session).
            summary = self.driver.run(COMPACT_PROMPT, session_id)
        if summary.is_error or not (summary.text or "").strip():
            log.warning("compaction summary failed for %s; keeping the session", conversation_id)
            self._note_compaction_failure(conversation_id)
            return False

        # Seed a fresh session outside the lock; it creates a new session id and
        # never touches the live one, so it must not hold up an incoming message.
        seeded = self.driver.run(SEED_TEMPLATE.format(summary=summary.text.strip()), None)
        if not (seeded.session_id and not seeded.is_error):
            log.warning("could not seed a fresh session for %s; keeping the old one", conversation_id)
            self._note_compaction_failure(conversation_id)
            return False

        with lock:
            # Compare-and-swap: only retire the exact session we summarized. If a
            # user turn advanced the conversation while we were seeding, keep it.
            if self.store.get(conversation_id) != session_id:
                log.info("conversation %s advanced during compaction; discarding the seed", conversation_id)
                return False
            self.store.set(conversation_id, seeded.session_id)
        self._compact_cooldown_until.pop(conversation_id, None)
        log.info("compacted conversation %s onto a fresh session", conversation_id)
        return True

    def reset(self, conversation_id: str) -> bool:
        """Forget a conversation so the next message starts fresh."""
        with self._locks_guard:
            self._epochs[conversation_id] = self._epochs.get(conversation_id, 0) + 1
        return self.store.clear(conversation_id)

    def _epoch_for(self, conversation_id: str) -> int:
        with self._locks_guard:
            return self._epochs.get(conversation_id, 0)

    @classmethod
    def from_config(cls, config: Config) -> "Agent":
        # The attachments dir must be reachable so the Read tool can open
        # downloaded images/files.
        add_dirs = list(config.add_dirs)
        if config.attachments_dir:
            add_dirs.append(config.attachments_dir)
        driver = ClaudeDriver(
            claude_bin=config.claude_bin,
            model=config.model,
            append_system_prompt_file=config.persona_file,
            mcp_config=config.mcp_config,
            permission_mode=config.permission_mode,
            allowed_tools=config.allowed_tools or None,
            disallowed_tools=config.disallowed_tools or None,
            restrict_builtin_tools=config.restrict_builtin_tools,
            disable_auto_memory=config.disable_auto_memory,
            add_dirs=add_dirs or None,
            timeout=config.turn_timeout,
            max_retries=config.max_retries,
            retry_base_delay=config.retry_base_delay,
            timeout_max_retries=config.timeout_max_retries,
        )
        store = SessionStore(config.session_store_path)
        stream_driver = None
        if config.live_interrupt:
            stream_driver = StreamDriver(
                driver,
                idle_timeout=config.stream_idle_timeout,
                total_timeout=config.stream_total_timeout,
            )
        return cls(
            driver,
            store,
            compact_every=config.compact_every,
            compact_at_tokens=config.compact_at_tokens,
            light_model=config.light_model,
            metrics_file=config.metrics_file,
            trivial_max_chars=config.trivial_max_chars,
            stream_driver=stream_driver,
        )


class LiveTurn:
    """One streaming turn, driven across its life: launch, redirect, finish.

    A live turn cannot be a single synchronous call the way :meth:`Agent.respond`
    is, because the user keeps talking into it while it runs. So its lifecycle is
    spread over a few awaits, but the invariants are the same as ``respond``:

    * The conversation's lock is held from the moment the session is read until
      the new session is written, for the *whole* turn. The runner already serializes
      turns; this lock additionally keeps a background compaction from resuming the
      same session in parallel and forking the transcript.
    * A resumed session that is dead or has overflowed is dropped and the turn is
      relaunched once on a fresh session, identically to ``respond``.
    * Compaction is decided inside the lock but launched outside it, so the user's
      reply is never delayed by it.

    Drive order: :meth:`begin`, then :meth:`inject` any number of times while
    :meth:`is_open`, then :meth:`result` for the reply, then :meth:`aftermath` for
    any stray follow-ups. :meth:`close` is idempotent and always releases the lock.
    """

    def __init__(self, agent: "Agent", conversation_id: str, prompt: str, model: Optional[str],
                 *, routed: str = "strong", reason: str = "", has_attachments: bool = False):
        self._agent = agent
        self._cid = conversation_id
        self._prompt = prompt
        self._model = model
        self._routed = routed
        self._reason = reason
        self._has_attachments = has_attachments
        self._epoch = agent._epoch_for(conversation_id)
        self._lock = agent._lock_for(conversation_id)
        self._have_lock = False
        self._turn: Optional[StreamTurn] = None
        self._final: Optional[ClaudeResult] = None
        self._due_to_compact = False
        self._done = False

    async def begin(self) -> None:
        """Acquire the conversation lock and launch the streaming process."""
        await asyncio.to_thread(self._lock.acquire)
        self._have_lock = True
        try:
            session_id = self._agent.store.get(self._cid)
            self._turn = await asyncio.to_thread(
                self._agent.stream_driver.start, self._prompt, session_id, self._model
            )
        except BaseException:
            self.close()
            raise

    def is_open(self) -> bool:
        return self._turn is not None and self._turn.open

    async def inject(self, text: str) -> bool:
        """Feed a follow-up into the running turn. False if it already closed."""
        if self._turn is None:
            return False
        return await asyncio.to_thread(self._turn.inject, text)

    async def result(self) -> ClaudeResult:
        """Await the reply, retrying once on a dead or overflowed session."""
        if self._final is not None:
            return self._final
        try:
            result = await asyncio.to_thread(self._resolve)
        except BaseException:
            # Never leak the conversation lock if persistence or a relaunch fails;
            # close() is idempotent, so the later aftermath/close is harmless.
            self.close()
            raise
        self._final = result
        return result

    def _resolve(self) -> ClaudeResult:
        turn = self._turn
        assert turn is not None
        # A success result is sendable the instant it lands; an error must wait
        # for the process to finish so stderr (the real cause) is folded in.
        result = turn.wait_primary()
        if result is None or result.is_error:
            turn.wait_finished()
            result = turn.wait_primary()

        session_id = self._agent.store.get(self._cid)
        if result is not None and result.is_error and session_id and (
            _is_dead_session(result) or _is_overflow(result)
        ):
            if _is_overflow(result):
                log.warning("conversation %s overflowed its context; starting fresh", self._cid)
            self._agent.store.clear(self._cid)
            turn = self._agent.stream_driver.start(self._prompt, None, self._model)
            self._turn = turn
            turn.wait_finished()
            result = turn.wait_primary()

        if result is None:
            result = ClaudeResult(text="", session_id=session_id, is_error=True,
                                  error="live turn ended without a result")
        # Honor a reset that landed mid-turn: if the conversation's epoch advanced,
        # the user cleared it, so do not write this now-stale session back.
        if result.session_id and self._agent._epoch_for(self._cid) == self._epoch:
            self._agent.store.set(self._cid, result.session_id)
        emit_turn(
            self._agent.metrics_file, self._cid, result, self._routed, self._reason,
            self._has_attachments, self._agent.store.turns(self._cid),
        )
        self._due_to_compact = not result.is_error and self._agent._should_compact(self._cid, result)
        return result

    async def aftermath(self) -> list[ClaudeResult]:
        """Wait out the process, release the lock, kick off compaction, return strays."""
        try:
            turn = self._turn
            if turn is not None:
                await asyncio.to_thread(turn.wait_finished)
            strays = turn.strays if turn is not None else []
        finally:
            self.close()
        if self._due_to_compact:
            self._agent._launch_compaction(self._cid)
        return strays

    def close(self) -> None:
        """Release the conversation lock if held. Safe to call more than once."""
        if self._done:
            return
        self._done = True
        if self._have_lock:
            self._have_lock = False
            try:
                self._lock.release()
            except RuntimeError:
                pass
