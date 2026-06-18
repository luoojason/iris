"""Just-in-time approval: risk policy + the cross-process rendezvous store.

Claude Code's ``--permission-prompt-tool`` calls an MCP tool to decide whether a
tool use is allowed. This module is the brain behind that tool:

* ``needs_approval`` decides whether a given (tool, input) is risky enough to ask
  the owner — gating on ARGUMENTS, not just the tool name — so ordinary calls
  auto-allow and only the dangerous ones prompt.
* ``ApprovalStore`` is the rendezvous between the MCP server (a separate process
  that posts the request and waits) and the bot (which receives the owner's
  button tap and records the decision). flock-backed so the two processes share
  it safely.

The MCP server and the Discord ``on_interaction`` wiring live elsewhere; this is
the pure, testable core. Fail-closed (timeout -> deny) is enforced by the caller.
"""

from __future__ import annotations

import json
import uuid
from typing import Callable, Optional

from .statefile import JsonDictStore

# Grants powerful enough that launching a job with them deserves a human tap.
_POWERFUL_GRANTS = {"shell", "files", "browser"}

# Tools whose every call is risky regardless of arguments (public / irreversible).
_ALWAYS = {"mcp__publish__publish_video"}

# Job-launching tools whose risk depends on the requested grants / model.
_JOB_LAUNCHERS = {"mcp__jobs__start_job", "mcp__jobs__run_in_background", "mcp__jobs__schedule_job"}


def _grant_set(value) -> set:
    if isinstance(value, (list, tuple)):
        return {str(g).strip() for g in value if str(g).strip()}
    return {g.strip() for g in str(value or "").split(",") if g.strip()}


def needs_approval(tool_name: str, tool_input: dict, config) -> tuple[bool, str]:
    """Whether (tool, input) should require an owner tap. Returns (needed, reason).

    Extensible by design: add a rule here. Everything not matched auto-allows, so
    this never turns into nagware.
    """
    tool_input = tool_input or {}
    if tool_name in _ALWAYS:
        return True, "posts publicly / is irreversible"
    if tool_name in _JOB_LAUNCHERS:
        grants = _grant_set(tool_input.get("grants"))
        powerful = grants & _POWERFUL_GRANTS
        if powerful:
            return True, f"launches a job with {sorted(powerful)} access"
        if tool_input.get("heavy"):
            return True, "launches a heavy (expensive) job"
    return False, ""


class ApprovalStore:
    """flock-backed pending-approval requests, shared by the MCP server and the bot."""

    def __init__(self, path):
        self._store = JsonDictStore(path, "approvals")
        self.path = self._store.path

    def create(self, req_id: str, summary: str, *, now: float) -> None:
        with self._store.locked():
            data = self._store.load()
            data[req_id] = {"summary": summary, "decision": None, "by": None, "ts": now}
            self._store.save(data)

    def record(self, req_id: str, decision: str, *, by: str, now: float) -> bool:
        """Record the first decision for a request. Returns True if it took effect,
        False if the request is unknown or already decided (first tap wins)."""
        with self._store.locked():
            data = self._store.load()
            entry = data.get(req_id)
            if entry is None or entry.get("decision") is not None:
                return False
            entry.update(decision=decision, by=by, decided_ts=now)
            self._store.save(data)
            return True

    def get(self, req_id: str) -> Optional[dict]:
        return self._store.load().get(req_id)

    def prune(self, *, before_ts: float) -> int:
        """Drop decided/old requests so the file does not grow without bound."""
        with self._store.locked():
            data = self._store.load()
            drop = [k for k, v in data.items() if (v.get("ts") or 0) < before_ts]
            for k in drop:
                del data[k]
            if drop:
                self._store.save(data)
            return len(drop)


def format_decision(behavior: str, message: str = "") -> str:
    """The JSON string Claude Code's --permission-prompt-tool expects back."""
    if behavior == "allow":
        return json.dumps({"behavior": "allow"})
    return json.dumps({"behavior": "deny", "message": message or "denied"})


def decide(tool_name: str, tool_input: dict, config, *, store: ApprovalStore,
           post: Callable[[str, str], bool], now_fn: Callable[[], float],
           sleep_fn: Optional[Callable[[float], None]] = None,
           timeout: Optional[float] = None, poll_secs: float = 2.0,
           req_id: Optional[str] = None) -> str:
    """Allow auto-safe calls; otherwise ask the owner and wait, failing closed.

    Pure of I/O specifics: ``post`` sends the Approve/Deny prompt (returns False if
    it could not reach the owner), ``store`` is the rendezvous the bot writes the
    tap into, and ``now_fn``/``sleep_fn`` are injected so the poll loop is testable.
    Returns the Claude Code permission-tool JSON.
    """
    needed, reason = needs_approval(tool_name, tool_input, config)
    if not needed:
        return format_decision("allow")
    req_id = req_id or uuid.uuid4().hex
    timeout = timeout if timeout is not None else getattr(config, "approval_timeout", 300.0)
    sleep_fn = sleep_fn or (lambda _s: None)
    summary = f"{tool_name} — {reason}"
    store.create(req_id, summary, now=now_fn())
    if not post(req_id, summary):
        return format_decision("deny", "could not reach you to ask; denied (fail closed)")
    deadline = now_fn() + timeout
    while now_fn() < deadline:
        entry = store.get(req_id)
        decision = entry.get("decision") if entry else None
        if decision == "allow":
            return format_decision("allow")
        if decision == "deny":
            return format_decision("deny", "denied by owner")
        sleep_fn(poll_secs)
    return format_decision("deny", "no response in time; denied (fail closed)")
