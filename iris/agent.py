"""Transport-agnostic agent core: turn a message into a reply.

Every front end (Discord, Telegram, the terminal) does the same three things on
each message: look up this conversation's session, run the brain, remember the
new session id. That lives here, once, so adding a transport is just wiring.
"""

from __future__ import annotations

import threading

from .config import Config
from .driver import ClaudeDriver, ClaudeResult
from .sessions import SessionStore


def _is_dead_session(result: ClaudeResult) -> bool:
    """True when an error means the resumed claude session no longer exists."""
    blob = (result.error or "").lower()
    return "no conversation found" in blob or ("session" in blob and "not found" in blob)


class Agent:
    def __init__(self, driver: ClaudeDriver, store: SessionStore):
        self.driver = driver
        self.store = store
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, conversation_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(conversation_id, threading.Lock())

    def respond(self, conversation_id: str, text: str) -> ClaudeResult:
        """Run one turn for a conversation and persist its session id.

        Turns for the same conversation are serialized so two messages arriving
        at once do not run concurrent --resume turns and fork the transcript.
        """
        with self._lock_for(conversation_id):
            session_id = self.store.get(conversation_id)
            result = self.driver.run(text, session_id)
            # A resumed session that no longer exists carries no replacement id,
            # so the dead id would be retried forever. Heal it: drop and retry.
            if result.is_error and session_id and _is_dead_session(result):
                self.store.clear(conversation_id)
                result = self.driver.run(text, None)
            if result.session_id:
                self.store.set(conversation_id, result.session_id)
            return result

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
        return cls(driver, store)
