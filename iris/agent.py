"""Transport-agnostic agent core: turn a message into a reply.

Every front end (Discord, Telegram, the terminal) does the same three things on
each message: look up this conversation's session, run the brain, remember the
new session id. That lives here, once, so adding a transport is just wiring.
"""

from __future__ import annotations

from .config import Config
from .driver import ClaudeDriver, ClaudeResult
from .sessions import SessionStore


class Agent:
    def __init__(self, driver: ClaudeDriver, store: SessionStore):
        self.driver = driver
        self.store = store

    def respond(self, conversation_id: str, text: str) -> ClaudeResult:
        """Run one turn for a conversation and persist its session id."""
        result = self.driver.run(text, self.store.get(conversation_id))
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
            system_prompt_file=config.persona_file,
            mcp_config=config.mcp_config,
            permission_mode=config.permission_mode,
            allowed_tools=config.allowed_tools or None,
            disallowed_tools=config.disallowed_tools or None,
            add_dirs=config.add_dirs or None,
            timeout=config.turn_timeout,
        )
        store = SessionStore(config.session_store_path)
        return cls(driver, store)
