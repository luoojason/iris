"""Iris: a personal chat agent that runs on your Claude subscription.

Iris uses the official ``claude`` binary (Claude Code) in headless mode as its
brain, so it costs nothing beyond your existing Claude Pro or Max plan and stays
within Anthropic's terms. It is a Claude-subscription-native alternative to the
Hermes agent, with its messaging, persona, memory, and tools rebuilt on Claude
Code's own extension points (MCP, sessions, system prompts).
"""

from __future__ import annotations

from .agent import Agent
from .config import Config
from .driver import ClaudeDriver, ClaudeError, ClaudeResult
from .sessions import SessionStore

__version__ = "0.1.0"

__all__ = [
    "Agent",
    "Config",
    "ClaudeDriver",
    "ClaudeError",
    "ClaudeResult",
    "SessionStore",
    "__version__",
]
