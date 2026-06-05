"""Run Claude Code headlessly as the brain for a chat agent.

The driver shells out to the official ``claude`` binary in print mode
(``claude -p --output-format json``). That binary runs on the user's Claude
subscription. There is no API key and no impersonation: this is Anthropic's
own client doing exactly what its documented headless mode exists for, which
keeps the whole setup inside the subscription's terms.

Continuity, persona, and custom tools are all native ``claude`` features:

* ``--resume <session_id>`` keeps a conversation going across turns.
* ``--system-prompt-file`` / ``--append-system-prompt`` set the persona.
* ``--mcp-config`` hands Claude custom tools over the Model Context Protocol.
* ``--permission-mode`` / ``--allowedTools`` keep tool use under control.
"""

from __future__ import annotations

import json
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
    raw: dict = field(default_factory=dict)


# A runner takes (command, timeout) and returns something with ``.returncode``,
# ``.stdout`` and ``.stderr``. The default uses subprocess; tests inject a fake.
Runner = Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"]


def _default_runner(cmd: Sequence[str], timeout: float):
    return subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout)


_RATE_LIMIT_MARKERS = ("rate_limit", "rate limit", "429", "overloaded", "529")


@dataclass
class ClaudeDriver:
    """Build and run ``claude -p`` commands and parse their JSON output."""

    claude_bin: str = "claude"
    model: Optional[str] = None
    system_prompt_file: Optional[str] = None
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

    def build_command(self, prompt: str, session_id: Optional[str] = None) -> list[str]:
        """Assemble the argv for one turn. Pure and side-effect free."""
        cmd: list[str] = [self.claude_bin, "-p", prompt, "--output-format", "json"]
        if session_id:
            cmd += ["--resume", session_id]
        if self.model:
            cmd += ["--model", self.model]
        if self.system_prompt_file:
            cmd += ["--system-prompt-file", self.system_prompt_file]
        if self.append_system_prompt:
            cmd += ["--append-system-prompt", self.append_system_prompt]
        if self.mcp_config:
            cmd += ["--mcp-config", self.mcp_config]
        if self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        if self.allowed_tools:
            cmd += ["--allowedTools", *self.allowed_tools]
        if self.disallowed_tools:
            cmd += ["--disallowedTools", *self.disallowed_tools]
        for directory in self.add_dirs or []:
            cmd += ["--add-dir", directory]
        return cmd

    def run(self, prompt: str, session_id: Optional[str] = None) -> ClaudeResult:
        """Run one turn, retrying transient failures (rate limits, timeouts)."""
        if self.runner is _default_runner and shutil.which(self.claude_bin) is None:
            raise ClaudeError(
                f"claude binary not found on PATH: {self.claude_bin!r}. "
                "Install Claude Code and sign in to your subscription first."
            )

        cmd = self.build_command(prompt, session_id)
        last_error: Optional[str] = None

        for attempt in range(self.max_retries + 1):
            try:
                proc = self.runner(cmd, self.timeout)
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
