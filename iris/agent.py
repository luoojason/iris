"""Transport-agnostic agent core: turn a message into a reply.

Every front end (Discord, Telegram, the terminal) does the same three things on
each message: look up this conversation's session, run the brain, remember the
new session id. That lives here, once, so adding a transport is just wiring.
"""

from __future__ import annotations

import logging
import threading

from .config import Config
from .driver import ClaudeDriver, ClaudeResult
from .router import choose_model
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
    ):
        self.driver = driver
        self.store = store
        # When set, trivial turns are routed to this lighter model to save credit;
        # everything else uses the driver's default (strong) model.
        self.light_model = light_model
        # Compact a conversation after this many turns on one session (0 = never).
        # A coarse backstop; the token threshold below is the accurate trigger.
        self.compact_every = compact_every
        # Compact once a turn's context reaches this many tokens (0 = never). This
        # catches tool-heavy turns (a big web fetch) that turn-count would miss.
        self.compact_at_tokens = compact_at_tokens
        # When False, compaction runs inline instead of in a background thread
        # (used by tests for determinism).
        self.compact_async = True
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._compacting: set[str] = set()
        self._last_compaction: threading.Thread | None = None

    def _lock_for(self, conversation_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(conversation_id, threading.Lock())

    def respond(self, conversation_id: str, text: str, has_attachments: bool = False) -> ClaudeResult:
        """Run one turn for a conversation and persist its session id.

        Turns for the same conversation are serialized so two messages arriving
        at once do not run concurrent --resume turns and fork the transcript.
        """
        model = choose_model(
            text,
            light_model=self.light_model or None,
            has_attachments=has_attachments,
        )
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
            due_to_compact = not result.is_error and self._should_compact(conversation_id, result)
        # Outside the lock: the user already has their reply. Compaction runs on
        # its own and re-acquires the lock, so it never delays this turn.
        if due_to_compact:
            self._launch_compaction(conversation_id)
        return result

    def _should_compact(self, conversation_id: str, result: ClaudeResult) -> bool:
        """Whether this conversation is big enough to condense onto a fresh session."""
        if self.compact_at_tokens > 0 and (result.context_tokens or 0) >= self.compact_at_tokens:
            return True
        if self.compact_every > 0 and self.store.turns(conversation_id) >= self.compact_every:
            return True
        return False

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
        with self._lock_for(conversation_id):
            session_id = self.store.get(conversation_id)
            if not session_id:
                return False
            summary = self.driver.run(COMPACT_PROMPT, session_id)
            if summary.is_error or not (summary.text or "").strip():
                log.warning("compaction summary failed for %s; keeping the session", conversation_id)
                return False
            seeded = self.driver.run(SEED_TEMPLATE.format(summary=summary.text.strip()), None)
            if seeded.session_id and not seeded.is_error:
                self.store.set(conversation_id, seeded.session_id)
                log.info("compacted conversation %s onto a fresh session", conversation_id)
                return True
            log.warning("could not seed a fresh session for %s; keeping the old one", conversation_id)
            return False

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
            add_dirs=add_dirs or None,
            timeout=config.turn_timeout,
        )
        store = SessionStore(config.session_store_path)
        return cls(
            driver,
            store,
            compact_every=config.compact_every,
            compact_at_tokens=config.compact_at_tokens,
            light_model=config.light_model,
        )
