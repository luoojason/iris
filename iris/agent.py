"""Transport-agnostic agent core: turn a message into a reply.

Every front end (Discord, Telegram, the terminal) does the same three things on
each message: look up this conversation's session, run the brain, remember the
new session id. That lives here, once, so adding a transport is just wiring.
"""

from __future__ import annotations

import logging
import threading
import time

from .config import Config
from .driver import ClaudeDriver, ClaudeResult
from .metrics import emit_turn
from .router import choose_model_explained
from .sessions import SessionStore

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
    ):
        self.driver = driver
        self.store = store
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
        return self.store.clear(conversation_id)

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
        return cls(
            driver,
            store,
            compact_every=config.compact_every,
            compact_at_tokens=config.compact_at_tokens,
            light_model=config.light_model,
            metrics_file=config.metrics_file,
            trivial_max_chars=config.trivial_max_chars,
        )
