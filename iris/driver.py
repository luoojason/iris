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

One sharp edge to know: ``--allowedTools`` is an *approval* list, not an
availability list. Under ``--permission-mode default`` the host
``~/.claude/settings.json`` ``permissions.allow`` still pre-approves built-in
tools (Bash, Write, Edit, WebFetch, ...), so allowlisting only the MCP memory
tool does not actually keep the agent from running a shell. Iris closes that gap
by defaulting a ``--disallowedTools`` denylist of the dangerous built-ins (shell,
file writes, subagents; deny rules outrank allow rules, even host settings ones).
Set ``restrict_builtin_tools=False`` to opt out, or pass an explicit
``disallowed_tools`` to take full control.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

log = logging.getLogger("iris.driver")


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

# Built-in tools that give the agent real reach: a shell, file writes, and
# subagent spawning. Denied by default so IRIS_ALLOWED_TOOLS is an actual
# boundary and the prompt-injection-to-shell chain is closed. Read/Glob/Grep
# (read-only) and WebFetch/WebSearch (advertised features) stay available; deny
# them too with an explicit IRIS_DISALLOWED_TOOLS if you want defense-in-depth.
DANGEROUS_BUILTINS = (
    "Bash",
    "Write",
    "Edit",
    "NotebookEdit",
    "Task",
    "Agent",  # Task's new name since Claude Code 2.1.63; both still resolve
    "KillShell",
    "BashOutput",
)

# Secrets that must never reach the model's tool sandbox. IRIS_* holds the bot
# token; the ANTHROPIC_* keys, if exported on the host, could make the child
# bill against an API key instead of drawing the subscription's agent credit,
# silently breaking the subscription-native design.
_SECRET_ENV_DROP = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _child_env(disable_auto_memory: bool = False) -> dict:
    """Environment for the claude child, with Iris's own secrets removed.

    The agent process holds the bot token in ``IRIS_*`` vars and the host may
    export ``ANTHROPIC_*`` keys; neither must reach the model's tool sandbox.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("IRIS_") and k not in _SECRET_ENV_DROP
    }
    if disable_auto_memory:
        # Keep the model from writing native memory files under
        # ~/.claude/projects/<proj>/memory; the MCP memory tool is the store.
        env.setdefault("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "1")
    return env


def _default_runner(cmd: Sequence[str], timeout: float, prompt: str):
    """Sentinel default runner. Real runs go through ClaudeDriver._subprocess_run;
    this identity is what the driver checks to know it owns the subprocess."""
    raise RuntimeError("the default runner is dispatched by ClaudeDriver._subprocess_run")


# Rate-limit / overload responses: worth retrying after a backoff.
_RATE_LIMIT_MARKERS = ("rate_limit", "rate limit", "429", "overloaded", "529")

# Credit-pool exhaustion: the subscription itself pushed back, as opposed to a
# per-request defect (auth, bad request). Split out so credit exhaustion stays
# terminal (never retried) without lumping it in with the auth-shaped defects.
_CREDIT_MARKERS = ("credit balance", "insufficient", "quota")

# What parks the job fleet. Parking decides for every job at once and the text
# it reads is free-form (model prose, folded stderr), so only API-shaped
# pushback phrases qualify: the retry classifiers' bare 'insufficient'/'quota'
# would park on "insufficient permissions" or "disk quota exceeded".
_PUSHBACK_MARKERS = ("credit balance", "insufficient credit") + _RATE_LIMIT_MARKERS

# Permanent failures: retrying only wastes time and credit. Auth, bad request,
# and credit exhaustion all surface immediately instead of being hidden behind
# minutes of backoff.
_TERMINAL_MARKERS = _CREDIT_MARKERS + (
    "authentication_error",
    "permission_error",
    "invalid_request_error",
    "not_found_error",
)


def is_credit_or_rate_pushback(error_text: Optional[str]) -> bool:
    """True when an error text is the credit pool or a rate limit pushing back.

    The job runner parks claiming on these. API-shaped phrases only:
    per-request defects (auth, bad request) and free-form job error text that
    merely mentions 'insufficient' or 'quota' stay out, so one broken job
    cannot park the whole fleet.
    """
    blob = (error_text or "").lower()
    return any(marker in blob for marker in _PUSHBACK_MARKERS)


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
    # Deny the dangerous built-ins by default so the allowlist is a real boundary.
    restrict_builtin_tools: bool = True
    # Keep native auto-memory off so the bundled MCP memory tool is the store.
    disable_auto_memory: bool = True
    add_dirs: Optional[Sequence[str]] = None
    # Working directory for the claude child. None = inherit the bot's cwd
    # (exactly today's behavior). The job runner sets this from an owner-bound
    # workspace; the model itself never names a path, only a workspace name.
    cwd: Optional[str] = None
    timeout: float = 300.0
    # Transient failures (rate limit, overload, in-flight execution errors).
    max_retries: int = 2
    retry_base_delay: float = 2.0
    # Timeouts are a different failure class: a slow or hung turn rarely recovers
    # by waiting another full timeout, and a retry can re-run partial tool side
    # effects. So they are retried separately and default to "report at once".
    timeout_max_retries: int = 0
    runner: Runner = _default_runner
    sleep: Callable[[float], None] = time.sleep

    @property
    def _owns_subprocess(self) -> bool:
        return self.runner is _default_runner

    def _effective_disallowed(self) -> Optional[Sequence[str]]:
        if self.disallowed_tools:
            return self.disallowed_tools  # explicit denylist takes full control
        if self.restrict_builtin_tools:
            return DANGEROUS_BUILTINS
        return None

    def build_command(
        self,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        stream: bool = False,
    ) -> list[str]:
        """Assemble the argv for one turn. The prompt is NOT here; it goes on stdin.

        With ``stream=True`` the command uses the realtime stream-json transport
        (input and output), which is what the live-interrupt driver needs to feed
        messages into a turn while it runs. Everything else (resume, model,
        persona, tools, the built-in denylist) is identical, so the one-shot and
        streaming transports stay in lockstep, including the security hardening.
        """
        if stream:
            cmd: list[str] = [
                self.claude_bin, "-p",
                "--input-format", "stream-json",
                "--output-format", "stream-json",
                "--verbose",
            ]
        else:
            cmd = [self.claude_bin, "-p", "--output-format", "json"]
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
        disallowed = self._effective_disallowed()
        if disallowed:
            cmd += ["--disallowedTools", *disallowed]
        for directory in self.add_dirs or []:
            cmd += ["--add-dir", directory]
        return cmd

    def run(self, prompt: str, session_id: Optional[str] = None, model: Optional[str] = None) -> ClaudeResult:
        """Run one turn, retrying transient failures (rate limits, overload).

        ``model`` overrides the driver's default model for this turn only, which
        is how per-turn routing picks a lighter model for trivial messages.
        """
        if self._owns_subprocess and shutil.which(self.claude_bin) is None:
            raise ClaudeError(
                f"claude binary not found on PATH: {self.claude_bin!r}. "
                "Install Claude Code and sign in to your subscription first."
            )

        cmd = self.build_command(session_id, model)
        last_error: Optional[str] = None
        timeout_attempts = 0
        transient_attempts = 0

        while True:
            try:
                if self._owns_subprocess:
                    proc = self._subprocess_run(cmd, self.timeout, prompt)
                else:
                    proc = self.runner(cmd, self.timeout, prompt)
            except subprocess.TimeoutExpired:
                last_error = f"claude timed out after {self.timeout:.0f}s"
                if timeout_attempts < self.timeout_max_retries:
                    timeout_attempts += 1
                    log.warning(
                        "claude timed out after %.0fs; retry %d/%d",
                        self.timeout, timeout_attempts, self.timeout_max_retries,
                    )
                    self._backoff(timeout_attempts - 1)
                    continue
                log.warning("claude timed out after %.0fs; giving up", self.timeout)
                break
            except FileNotFoundError as exc:
                raise ClaudeError(str(exc)) from exc

            result = self._parse(proc, session_id)
            if not result.is_error:
                return result

            last_error = result.error
            if transient_attempts < self.max_retries and self._is_retryable(result):
                transient_attempts += 1
                rate_limited = self._is_rate_limited(result)
                log.warning(
                    "claude turn failed (%s); retry %d/%d",
                    result.error, transient_attempts, self.max_retries,
                )
                self._backoff(transient_attempts - 1, rate_limited=rate_limited)
                continue
            return result

        return ClaudeResult(
            text="", session_id=session_id, is_error=True,
            error=last_error or "claude failed for an unknown reason",
        )

    # -- internals ---------------------------------------------------------

    def _subprocess_run(self, cmd: Sequence[str], timeout: float, prompt: str):
        """Run claude in its own process group so a timeout kills the whole tree.

        ``subprocess.run`` would terminate only the direct child on timeout,
        orphaning anything claude spawned (MCP stdio servers, an in-flight Bash
        tool). Running in a new session lets us signal the group.
        """
        kwargs = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            list(cmd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_child_env(self.disable_auto_memory),
            cwd=self.cwd,
            **kwargs,
        )
        try:
            out, err = proc.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_tree(proc)
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            raise
        return subprocess.CompletedProcess(list(cmd), proc.returncode, out, err)

    @staticmethod
    def _kill_tree(proc: "subprocess.Popen") -> None:
        # Only attempt a process-group kill for a real, positive pid; otherwise
        # (a non-started or fake process) fall straight through to proc.kill().
        pid = getattr(proc, "pid", None)
        if os.name == "posix" and isinstance(pid, int) and pid > 0:
            try:
                os.killpg(os.getpgid(pid), 9)
                return
            except Exception:
                pass
        try:
            proc.kill()
        except Exception:
            pass

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
        stderr = (getattr(proc, "stderr", "") or "").strip()
        return parse_result_event(obj, session_id, returncode=returncode, stderr=stderr)

    def _is_rate_limited(self, result: ClaudeResult) -> bool:
        blob = f"{result.error or ''} {result.raw.get('api_error_status', '')}".lower()
        return any(marker in blob for marker in _RATE_LIMIT_MARKERS)

    def _is_terminal(self, result: ClaudeResult) -> bool:
        """A permanent failure (auth, bad request, credit exhaustion): never retry."""
        status = result.raw.get("api_error_status")
        if isinstance(status, int) and 400 <= status < 500 and status not in (408, 429):
            return True
        blob = f"{result.error or ''} {status or ''}".lower()
        return any(marker in blob for marker in _TERMINAL_MARKERS)

    def _is_retryable(self, result: ClaudeResult) -> bool:
        if self._is_rate_limited(result):
            return True
        if self._is_terminal(result):
            return False
        status = result.raw.get("api_error_status")
        if isinstance(status, int):
            return status >= 500
        return bool(status) or result.raw.get("subtype") == "error_during_execution"

    def _backoff(self, attempt: int, rate_limited: bool = False) -> None:
        delay = self.retry_base_delay * (2 ** attempt)
        if rate_limited:
            delay *= 4
        self.sleep(delay)


def parse_result_event(
    obj: dict,
    fallback_session_id: Optional[str] = None,
    *,
    returncode: int = 0,
    stderr: str = "",
    fold_stderr: bool = False,
) -> ClaudeResult:
    """Turn one ``result`` JSON object into a :class:`ClaudeResult`.

    Shared by both transports: the one-shot driver parses the single result it
    reads from stdout, and the streaming driver parses each ``result`` event off
    its event stream. ``stderr`` is folded into the error because the stream
    transport reports some failures (notably a dead ``--resume`` session, "No
    conversation found ...") only on stderr while the result event carries an
    empty error field; without the fold the dead-session and overflow retries
    upstream would never recognize the failure.
    """
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
        # fold_stderr is set only by the stream transport, where a dead --resume
        # session reports its cause on stderr while the result event's error field
        # is empty. The one-shot path leaves it off so its retry classifier never
        # sees appended stderr noise (which could flip a retryable failure to look
        # terminal), keeping that path byte-for-byte as before the merge.
        if fold_stderr and stderr and stderr not in (error or ""):
            error = f"{error}: {stderr}" if error else stderr

    return ClaudeResult(
        text=obj.get("result") or "",
        session_id=obj.get("session_id") or fallback_session_id,
        is_error=is_error,
        error=error,
        cost_usd=obj.get("total_cost_usd"),
        model=model,
        duration_ms=obj.get("duration_ms"),
        num_turns=obj.get("num_turns"),
        context_tokens=context_tokens,
        raw=obj,
    )


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
