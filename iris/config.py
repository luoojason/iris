"""Configuration, loaded from the environment (optionally seeded by a .env).

Kept to plain environment variables so the agent is easy to run anywhere a
shell can reach the ``claude`` binary: a laptop, a VPS, a systemd unit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    """Minimal .env reader: KEY=VALUE lines, ``#`` comments, no interpolation.

    Existing environment variables always win, so real env beats the file.
    """
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _split(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class Config:
    discord_token: str = ""
    telegram_token: str = ""
    # Restrict who the bot answers. Empty means "anyone in channels it sees".
    allowed_user_ids: list[str] = field(default_factory=list)
    # Only respond in these channel ids (empty = respond anywhere it is allowed).
    allowed_channel_ids: list[str] = field(default_factory=list)
    # Respond to every message in allowed channels, not just @mentions.
    respond_without_mention: bool = False

    claude_bin: str = "claude"
    model: Optional[str] = None
    # Optional lighter model for trivial turns (enables per-turn routing when set).
    light_model: str = ""
    persona_file: Optional[str] = None
    mcp_config: Optional[str] = None
    permission_mode: str = "default"
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    add_dirs: list[str] = field(default_factory=list)
    # Where inbound images/files are downloaded so the brain's Read tool can see them.
    attachments_dir: str = "iris-attachments"
    # A directory of skill folders (each with SKILL.md) to make available to the brain.
    skills_dir: str = ""
    # Transcribe inbound voice messages locally (needs the [voice] extra). Off by
    # default: the first voice message downloads a whisper model and runs CPU
    # inference, which can be slow on small hosts.
    voice_enabled: bool = False
    voice_model: str = "base"

    session_store_path: str = "iris-sessions.json"
    # When set, append one JSON line of telemetry per turn to this file. Opt-in;
    # empty means no metrics are written (the default for the published agent).
    metrics_file: str = ""
    turn_timeout: float = 300.0
    # Compact a conversation when a turn's context reaches this many tokens: the
    # accurate trigger, since it catches tool-heavy turns. 0 disables it.
    compact_at_tokens: int = 150000
    # Backstop trigger: also compact after this many turns on one session, in
    # case usage tokens are ever unavailable. 0 disables it.
    compact_every: int = 60

    @classmethod
    def from_env(cls, *, dotenv: str | os.PathLike[str] = ".env") -> "Config":
        load_dotenv(dotenv)
        return cls(
            discord_token=os.environ.get("IRIS_DISCORD_TOKEN", ""),
            telegram_token=os.environ.get("IRIS_TELEGRAM_TOKEN", ""),
            allowed_user_ids=_split(os.environ.get("IRIS_ALLOWED_USER_IDS")),
            allowed_channel_ids=_split(os.environ.get("IRIS_ALLOWED_CHANNEL_IDS")),
            respond_without_mention=_truthy(os.environ.get("IRIS_RESPOND_WITHOUT_MENTION")),
            claude_bin=os.environ.get("IRIS_CLAUDE_BIN", "claude"),
            model=os.environ.get("IRIS_MODEL") or None,
            light_model=os.environ.get("IRIS_MODEL_LIGHT", ""),
            persona_file=os.environ.get("IRIS_PERSONA_FILE") or None,
            mcp_config=os.environ.get("IRIS_MCP_CONFIG") or None,
            permission_mode=os.environ.get("IRIS_PERMISSION_MODE", "default"),
            allowed_tools=_split(os.environ.get("IRIS_ALLOWED_TOOLS")),
            disallowed_tools=_split(os.environ.get("IRIS_DISALLOWED_TOOLS")),
            add_dirs=_split(os.environ.get("IRIS_ADD_DIRS")),
            attachments_dir=os.environ.get("IRIS_ATTACHMENTS_DIR", "iris-attachments"),
            skills_dir=os.environ.get("IRIS_SKILLS_DIR", ""),
            voice_enabled=_truthy(os.environ.get("IRIS_VOICE")),
            voice_model=os.environ.get("IRIS_VOICE_MODEL", "base"),
            session_store_path=os.environ.get("IRIS_SESSION_STORE", "iris-sessions.json"),
            metrics_file=os.environ.get("IRIS_METRICS_FILE", ""),
            turn_timeout=float(os.environ.get("IRIS_TURN_TIMEOUT", "300")),
            compact_at_tokens=int(os.environ.get("IRIS_COMPACT_AT_TOKENS", "150000")),
            compact_every=int(os.environ.get("IRIS_COMPACT_EVERY", "60")),
        )


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}
