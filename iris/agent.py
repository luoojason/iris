"""Transport-agnostic agent core: turn a message into a reply.

Every front end (Discord, Telegram, the terminal) does the same three things on
each message: look up this conversation's session, run the brain, remember the
new session id. That lives here, once, so adding a transport is just wiring.
"""

from __future__ import annotations

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

    def respond(self, conversation_id: str, text: str) -> ClaudeResult:
        """Run one turn for a conversation and persist its session id."""
        session_id = self.store.get(conversation_id)
        result = self.driver.run(text, session_id)
        # A resumed session that no longer exists carries no replacement id, so
        # the dead id would be retried forever. Heal it: drop it and retry fresh.
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
        driver = ClaudeDriver(
            claude_bin=config.claude_bin,
            model=config.model,
            append_system_prompt_file=config.persona_file,
            mcp_config=config.mcp_config,
            permission_mode=config.permission_mode,
            allowed_tools=config.allowed_tools or None,
            disallowed_tools=config.disallowed_tools or None,
            add_dirs=config.add_dirs or None,
            timeout=config.turn_timeout,
        )
        store = SessionStore(config.session_store_path)
        return cls(driver, store)
