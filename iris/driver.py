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
from pathlib import Path
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
    "Agent",  # newer CLIs expose the subagent tool under this alias too
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


def _origin_channel(conversation_id: Optional[str]) -> Optional[str]:
    """The Discord channel/thread id behind a conversation id, or None.

    Only Discord conversations (``discord:<id>``) yield a routable channel; a CLI
    or other transport returns None so jobs fall back to the home channel.
    """
    if conversation_id and conversation_id.startswith("discord:"):
        return conversation_id.split(":", 1)[1] or None
    return None


def _default_runner(cmd: Sequence[str], timeout: float, prompt: str):
    """Sentinel default runner. Real runs go through ClaudeDriver._subprocess_run;
    this identity is what the driver checks to know it owns the subprocess."""
    raise RuntimeError("the default runner is dispatched by ClaudeDriver._subprocess_run")


# Rate-limit / overload responses: worth retrying after a backoff.
_RATE_LIMIT_MARKERS = ("rate_limit", "rate limit", "429", "overloaded", "529")

# Permanent failures: retrying only wastes time and credit. Auth, bad request,
# and credit exhaustion all surface immediately instead of being hidden behind
# minutes of backoff.
_TERMINAL_MARKERS = (
    "credit balance",
    "insufficient",
    "quota",
    "authentication_error",
    "permission_error",
    "invalid_request_error",
    "not_found_error",
)


@dataclass
class ClaudeDriver:
    """Build and run ``claude -p`` commands and parse their JSON output."""

    claude_bin: str = "claude"
    model: Optional[str] = None
    append_system_prompt_file: Optional[str] = None
    append_system_prompt: Optional[str] = None
    # Owner-edited rules appended to the system prompt on every turn (standing
    # orders: durable behavior, not facts). Re-read per command build, so edits
    # take effect on the next turn with no restart.
    standing_orders_file: Optional[str] = None
    # Called on every command build for an extra system-prompt block (the agent
    # wires the pinned-memory digest here). Must be cheap; a raising supplier is
    # logged and skipped so it can never break a turn.
    system_prompt_extra: Optional[Callable[[Optional[str]], Optional[str]]] = None
    mcp_config: Optional[str] = None
    permission_mode: str = "default"
    allowed_tools: Optional[Sequence[str]] = None
    disallowed_tools: Optional[Sequence[str]] = None
    # Deny the dangerous built-ins by default so the allowlist is a real boundary.
    restrict_builtin_tools: bool = True
    # Keep native auto-memory off so the bundled MCP memory tool is the store.
    disable_auto_memory: bool = True
    add_dirs: Optional[Sequence[str]] = None
    # Working directory for the claude child. None inherits this process's cwd
    # (the chat default). The job runner points it away from the agent's own
    # directory, which holds .env and state files the Read tool must not see.
    cwd: Optional[str] = None
    # Called with the claude child's pid right after spawn. The job runner uses
    # it to record the pid so a cancel can kill the whole claude tree (the
    # child runs in its own session, so killing the runner alone orphans it).
    child_pid_callback: Optional[Callable[[int], None]] = None
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
    # When set, claude routes every permission-needing tool use through this MCP
    # tool (the approvals server) for a just-in-time allow/deny. Off by default.
    permission_prompt_tool: Optional[str] = None
    # Trace ledger: when trace_file is set, every invocation appends one record
    # (kind, model, outcome, error category, timings, turns, tokens, cost) at this
    # single choke point. Content (prompt/reply/raw error) is kept only when
    # trace_capture_content is true. Off by default; fail-soft.
    trace_file: str = ""
    trace_kind: str = "chat"
    trace_capture_content: bool = False

    @property
    def _owns_subprocess(self) -> bool:
        return self.runner is _default_runner

    def _traced(self, result: "ClaudeResult", prompt: str,
                session_id: Optional[str]) -> "ClaudeResult":
        """Append a trace record for this invocation's final result, then return it."""
        if self.trace_file:
            from .trace import record_trace
            record_trace(self.trace_file, self.trace_kind, result, prompt=prompt,
                         session_id=session_id, capture_content=self.trace_capture_content)
        return result

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
        conversation_id: Optional[str] = None,
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
        # The CLI rejects --append-system-prompt and --append-system-prompt-file
        # together ("Cannot use both ... Please use only one."), so never emit
        # both. When there is inline content (standing orders / the pinned
        # digest), fold the persona file into it and pass one inline flag;
        # otherwise keep the persona on the file flag (out of argv).
        extra = self._append_system_prompt_value(conversation_id)
        if extra and self.append_system_prompt_file:
            persona = self._read_text(self.append_system_prompt_file)
            merged = "\n\n".join(p for p in (persona, extra) if p)
            if merged:
                cmd += ["--append-system-prompt", merged]
        elif extra:
            cmd += ["--append-system-prompt", extra]
        elif self.append_system_prompt_file:
            cmd += ["--append-system-prompt-file", self.append_system_prompt_file]
        if self.mcp_config:
            # --strict-mcp-config so the bot uses only our tools, not whatever
            # MCP servers the operator happens to have in ~/.claude.json.
            cmd += ["--mcp-config", self.mcp_config, "--strict-mcp-config"]
        if self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        if self.permission_prompt_tool:
            cmd += ["--permission-prompt-tool", self.permission_prompt_tool]
        if self.allowed_tools:
            cmd += ["--allowedTools", *self.allowed_tools]
        disallowed = self._effective_disallowed()
        if disallowed:
            cmd += ["--disallowedTools", *disallowed]
        for directory in self.add_dirs or []:
            cmd += ["--add-dir", directory]
        return cmd

    @staticmethod
    def _read_text(path: str) -> str:
        """Read a system-prompt file, degrading an unreadable file to ''."""
        try:
            return Path(path).read_text("utf-8").strip()
        except (OSError, UnicodeDecodeError):
            log.warning("system prompt file unreadable: %s", path)
            return ""

    def _append_system_prompt_value(self, conversation_id: Optional[str] = None) -> Optional[str]:
        """The merged ``--append-system-prompt`` value: static text + standing orders.

        Merged into one flag value (the CLI takes the option once); the standing
        orders file is read fresh on every call so the owner can edit it live.
        An unreadable file degrades to a warning, never a failed turn. The extra
        supplier (the tier-0 digests) is passed the current conversation so the
        pinned-memory block can be scoped to this thread.
        """
        parts = []
        if self.append_system_prompt:
            parts.append(self.append_system_prompt)
        if self.standing_orders_file:
            text = self._read_text(self.standing_orders_file)
            if text:
                parts.append(text)
        if self.system_prompt_extra is not None:
            try:
                extra = (self.system_prompt_extra(conversation_id) or "").strip()
            except Exception:
                log.warning("system prompt extra supplier failed", exc_info=True)
                extra = ""
            if extra:
                parts.append(extra)
        return "\n\n".join(parts) if parts else None

    def run(self, prompt: str, session_id: Optional[str] = None, model: Optional[str] = None,
            conversation_id: Optional[str] = None) -> ClaudeResult:
        """Run one turn, retrying transient failures (rate limits, overload).

        ``model`` overrides the driver's default model for this turn only, which
        is how per-turn routing picks a lighter model for trivial messages.
        ``conversation_id`` is the originating chat (e.g. a Discord thread); it is
        passed to the child as IRIS_ORIGIN_CHANNEL so a job started in this turn
        reports back to THIS thread instead of always the home channel.
        """
        if self._owns_subprocess and shutil.which(self.claude_bin) is None:
            raise ClaudeError(
                f"claude binary not found on PATH: {self.claude_bin!r}. "
                "Install Claude Code and sign in to your subscription first."
            )

        cmd = self.build_command(session_id, model, conversation_id=conversation_id)
        last_error: Optional[str] = None
        timeout_attempts = 0
        transient_attempts = 0

        while True:
            try:
                if self._owns_subprocess:
                    proc = self._subprocess_run(cmd, self.timeout, prompt, conversation_id)
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
                return self._traced(result, prompt, session_id)

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
            return self._traced(result, prompt, session_id)

        return self._traced(
            ClaudeResult(
                text="", session_id=session_id, is_error=True,
                error=last_error or "claude failed for an unknown reason",
            ),
            prompt, session_id,
        )

    # -- internals ---------------------------------------------------------

    def _subprocess_run(self, cmd: Sequence[str], timeout: float, prompt: str,
                        conversation_id: Optional[str] = None):
        """Run claude in its own process group so a timeout kills the whole tree.

        ``subprocess.run`` would terminate only the direct child on timeout,
        orphaning anything claude spawned (MCP stdio servers, an in-flight Bash
        tool). Running in a new session lets us signal the group.
        """
        kwargs = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        env = _child_env(self.disable_auto_memory)
        origin = _origin_channel(conversation_id)
        if origin:
            # Added AFTER the IRIS_* strip, so the job MCP server (which the child
            # spawns) can route a job's report back to the originating thread.
            env["IRIS_ORIGIN_CHANNEL"] = origin
        proc = subprocess.Popen(
            list(cmd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=self.cwd,
            **kwargs,
        )
        if self.child_pid_callback is not None:
            try:
                self.child_pid_callback(proc.pid)
            except Exception:
                log.warning("child pid callback failed", exc_info=True)
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
