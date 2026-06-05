"""Run Claude Code headlessly as the brain for a chat agent.

The driver shells out to the official ``claude`` binary in print mode
(``claude -p --output-format json``). That binary runs on the user's Claude
subscription. There is no API key and no impersonation: this is Anthropic's
own client doing exactly what its documented headless mode exists for, which
keeps the whole setup inside the subscription's terms.

The user's message is fed to ``claude`` on **stdin**, not as a command-line
argument. That keeps a message beginning with ``-`` from being parsed as a flag,
stops a crafted message from injecting flags, avoids the OS argument-length
limit on long pastes, and keeps the message out of the process list.

Continuity, persona, and custom tools are all native ``claude`` features:

* ``--resume <session_id>`` keeps a conversation going across turns.
* ``--append-system-prompt-file`` adds the persona on top of Claude Code's own
  system prompt (replacing it would strip Claude Code's tool instructions).
* ``--mcp-config`` (+ ``--strict-mcp-config``) hands Claude custom tools.
* ``--permission-mode`` / ``--allowedTools`` keep tool use under control.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence


class ClaudeError(RuntimeError):
    """The ``claude`` binary could not be run at all (missing, unusable)."""


@dataclass
class ClaudeResult:
    """The outcome of one ``claude -p`` turn."""

    text: str
    session_id: Optional[str]
    is_error: bool
    error: Optional[str] = None
    cost_usd: Optional[float] = None
    model: Optional[str] = None
    duration_ms: Optional[int] = None
    num_turns: Optional[int] = None
    # Total prompt tokens this turn carried (fresh + cache read + cache write).
    # This is how full the context window was, used to decide when to compact.
    context_tokens: Optional[int] = None
    raw: dict = field(default_factory=dict)


# A runner takes (command, timeout, prompt) and returns something with
# ``.returncode``, ``.stdout`` and ``.stderr``. The prompt is fed on stdin.
# The default uses subprocess; tests inject a fake.
Runner = Callable[[Sequence[str], float, str], "subprocess.CompletedProcess[str]"]


def _child_env() -> dict:
    """Environment for the claude child, with Iris's own secrets removed.

    The agent process holds the bot token in ``IRIS_*`` vars; those must never
    reach the model's tool sandbox (a shell tool could otherwise read them).
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("IRIS_")}


def _default_runner(cmd: Sequence[str], timeout: float, prompt: str):
    return subprocess.run(
        list(cmd),
        capture_output=True,
        text=True,
        timeout=timeout,
        input=prompt,
        env=_child_env(),
    )


_RATE_LIMIT_MARKERS = ("rate_limit", "rate limit", "429", "overloaded", "529")


@dataclass
class ClaudeDriver:
    """Build and run ``claude -p`` commands and parse their JSON output."""

    claude_bin: str = "claude"
    model: Optional[str] = None
    append_system_prompt_file: Optional[str] = None
    append_system_prompt: Optional[str] = None
    mcp_config: Optional[str] = None
    permission_mode: str = "default"
    allowed_tools: Optional[Sequence[str]] = None
    disallowed_tools: Optional[Sequence[str]] = None
    add_dirs: Optional[Sequence[str]] = None
    timeout: float = 300.0
    max_retries: int = 2
    retry_base_delay: float = 2.0
    runner: Runner = _default_runner
    sleep: Callable[[float], None] = time.sleep

    def build_command(self, session_id: Optional[str] = None, model: Optional[str] = None) -> list[str]:
        """Assemble the argv for one turn. The prompt is NOT here; it goes on stdin."""
        cmd: list[str] = [self.claude_bin, "-p", "--output-format", "json"]
        if session_id:
            cmd += ["--resume", session_id]
        chosen_model = model or self.model
        if chosen_model:
            cmd += ["--model", chosen_model]
        if self.append_system_prompt_file:
            cmd += ["--append-system-prompt-file", self.append_system_prompt_file]
        if self.append_system_prompt:
            cmd += ["--append-system-prompt", self.append_system_prompt]
        if self.mcp_config:
            # --strict-mcp-config so the bot uses only our tools, not whatever
            # MCP servers the operator happens to have in ~/.claude.json.
            cmd += ["--mcp-config", self.mcp_config, "--strict-mcp-config"]
        if self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        if self.allowed_tools:
            cmd += ["--allowedTools", *self.allowed_tools]
        if self.disallowed_tools:
            cmd += ["--disallowedTools", *self.disallowed_tools]
        for directory in self.add_dirs or []:
            cmd += ["--add-dir", directory]
        return cmd

    def run(self, prompt: str, session_id: Optional[str] = None, model: Optional[str] = None) -> ClaudeResult:
        """Run one turn, retrying transient failures (rate limits, timeouts).

        ``model`` overrides the driver's default model for this turn only, which
        is how per-turn routing picks a lighter model for trivial messages.
        """
        if self.runner is _default_runner and shutil.which(self.claude_bin) is None:
            raise ClaudeError(
                f"claude binary not found on PATH: {self.claude_bin!r}. "
                "Install Claude Code and sign in to your subscription first."
            )

        cmd = self.build_command(session_id, model)
        last_error: Optional[str] = None

        for attempt in range(self.max_retries + 1):
            try:
                proc = self.runner(cmd, self.timeout, prompt)
            except subprocess.TimeoutExpired:
                last_error = f"claude timed out after {self.timeout:.0f}s"
                if attempt < self.max_retries:
                    self._backoff(attempt)
                    continue
                break
            except FileNotFoundError as exc:
                raise ClaudeError(str(exc)) from exc

            result = self._parse(proc, session_id)
            if not result.is_error:
                return result

            last_error = result.error
            if attempt < self.max_retries and self._is_retryable(result):
                self._backoff(attempt, rate_limited=self._is_rate_limited(result))
                continue
            return result

        return ClaudeResult(
            text="", session_id=session_id, is_error=True,
            error=last_error or "claude failed for an unknown reason",
        )

    # -- internals ---------------------------------------------------------

    def _parse(self, proc, session_id: Optional[str]) -> ClaudeResult:
        out = (getattr(proc, "stdout", "") or "").strip()
        obj = _loads_result(out)
        if obj is None:
            stderr = (getattr(proc, "stderr", "") or "").strip()
            rc = getattr(proc, "returncode", None)
            return ClaudeResult(
                text="", session_id=session_id, is_error=True,
                error=stderr or f"claude exited {rc} with no parseable JSON output",
                raw={"stdout": out, "stderr": stderr, "returncode": rc},
            )

        returncode = getattr(proc, "returncode", 0) or 0
        is_error = (
            bool(obj.get("is_error"))
            or obj.get("subtype") not in (None, "success")
            or returncode != 0
        )
        model = None
        usage = obj.get("modelUsage")
        if isinstance(usage, dict) and usage:
            model = next(iter(usage))

        context_tokens = _context_tokens(obj.get("usage"))

        error = None
        if is_error:
            error = (
                obj.get("error")
                or obj.get("result")
                or obj.get("subtype")
                or f"claude exited {returncode}"
            )

        return ClaudeResult(
            text=obj.get("result") or "",
            session_id=obj.get("session_id") or session_id,
            is_error=is_error,
            error=error,
            cost_usd=obj.get("total_cost_usd"),
            model=model,
            duration_ms=obj.get("duration_ms"),
            num_turns=obj.get("num_turns"),
            context_tokens=context_tokens,
            raw=obj,
        )

    def _is_rate_limited(self, result: ClaudeResult) -> bool:
        blob = f"{result.error or ''} {result.raw.get('api_error_status', '')}".lower()
        return any(marker in blob for marker in _RATE_LIMIT_MARKERS)

    def _is_retryable(self, result: ClaudeResult) -> bool:
        if self._is_rate_limited(result):
            return True
        return bool(result.raw.get("api_error_status")) or result.raw.get("subtype") == "error_during_execution"

    def _backoff(self, attempt: int, rate_limited: bool = False) -> None:
        delay = self.retry_base_delay * (2 ** attempt)
        if rate_limited:
            delay *= 4
        self.sleep(delay)


def _context_tokens(usage) -> Optional[int]:
    """How many prompt tokens a turn carried, from the result's usage block.

    The full context the model saw is the fresh input plus everything served
    from (or written to) the prompt cache, so all three are summed.
    """
    if not isinstance(usage, dict):
        return None
    total = 0
    found = False
    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            total += int(value)
            found = True
    return total if found else None


def _loads_result(text: str) -> Optional[dict]:
    """Parse the result JSON, tolerating leading log noise before the object."""
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                continue
    return None
