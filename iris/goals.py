"""The goal loop: a standing objective the clock advances until it is done.

This is the fourth bounded relaxation of zero-idle-inference (after scheduled
jobs, autonomous resume, and the proactive reviews). The owner records a goal in
chat ("your goal is to ...") and from then on a cron tick advances it one work
step at a time, on its own continuous session, until the goal is reached, it
needs the owner, or it exhausts a per-goal step budget. It is never inference
from nothing: a goal exists only because the owner set it, and the same real
weekly-usage leash that gates the proactive reviews gates this — a step runs only
while there is headroom on the shared Max limit and the credit guard is unparked.

What makes it safe to let the clock push work forward:

* Independent judge. After each step the worker reports what it did; a separate,
  cheap-model check (no tools, fresh session) decides done / blocked / continue.
  The worker cannot mark its own goal done — a skeptical second model must agree.
* Independent verify on done. The judge rules on the worker's self-report, so a
  "done" verdict is then re-checked by an independent read-only turn that inspects
  the actual work (the wiki page, the memory, the page). It fires only on done, so
  it costs at most one cheap call per completion; an unconfirmed or erroring verify
  asks the owner instead of accepting a "done" the work doesn't back up.
* Fail-open, never wedge. If the judge errors, the tick asks the owner instead of
  silently looping or claiming success.
* Per-goal step budget. A goal that never converges hits ``max_steps``, stops,
  and asks the owner rather than burning credit forever.
* Owner routing. Done/blocked reports go to the thread the goal was set in (or the
  home channel), so an answer lands where the owner is looking.
* One goal per tick, least-recently-worked first, so many goals share the clock
  fairly and a single tick never fans out unbounded spend.

See docs/superpowers/specs/2026-06-14-goal-loop-design.md.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional

from .statefile import JsonListStore

log = logging.getLogger("iris.goals")

# Terminal states a goal can rest in; everything else ("active") keeps cycling.
TERMINAL = ("done", "blocked", "cancelled")

STEP_PROMPT = (
    "You are advancing a standing goal Jason set for you. The clock triggered "
    "this work session; Jason did not just ask. This is one step on the goal, "
    "not the whole thing.\n\n"
    "GOAL: {text}\n\n"
    "This is step {n} of up to {max} you have to reach it. Do the single most "
    "useful next thing toward the goal with your tools, then report briefly:\n"
    "- what you did this step and what changed,\n"
    "- whether the goal is now fully achieved (not just attempted),\n"
    "- anything you need from Jason to keep going.\n"
    "Be concise and do not repeat work from earlier steps."
)

VERIFY_PROMPT = (
    "An autonomous worker reported that this goal is now COMPLETE:\n\n"
    "GOAL: {text}\n\n"
    "The worker's report:\n\n{step}\n\n"
    "You are an independent verifier. Do NOT take the report at face value. Using "
    "your read-only tools, check the actual evidence — read the wiki page it claims "
    "to have written, recall the memory it says it saved, fetch the page, look at "
    "what was actually produced. Then decide whether the goal is genuinely and "
    "verifiably achieved. Reply with ONE line beginning with exactly one of:\n"
    "CONFIRMED: <the evidence you actually checked that proves it>\n"
    "UNCONFIRMED: <what is missing or could not be verified>\n"
    "Say CONFIRMED only if you verified the work yourself, not merely that the worker "
    "claims it. Do not change anything; only read."
)

JUDGE_PROMPT = (
    "A background worker is pursuing this goal for its owner:\n\n"
    "GOAL: {text}\n\n"
    "After a work step it reported:\n\n"
    "{step}\n\n"
    "You are an independent checker with no stake in the outcome. Decide the "
    "goal's state and reply with ONE line beginning with exactly one of:\n"
    "DONE: <short reason> - the goal is fully and verifiably achieved.\n"
    "BLOCKED: <short reason> - it cannot proceed without the owner (a decision, "
    "a credential, a choice only they can make).\n"
    "CONTINUE: <short reason> - more work will get there; let it keep going.\n"
    "Be skeptical of DONE: say it only if the report shows the goal actually met."
)


class GoalStore:
    """A file-backed list of goals, with the same flock + atomic-replace pattern
    as the inbox and resume queue so the chat process (which sets goals) and the
    cron tick (which advances them) never tear the file."""

    def __init__(self, path: str | os.PathLike[str]):
        self._store = JsonListStore(path, "goals")
        self.path = self._store.path

    @contextmanager
    def _locked(self):
        with self._store.locked():
            yield

    def _load(self) -> list[dict]:
        return [item for item in self._store.load() if isinstance(item, dict)]

    def _save(self, items: list[dict]) -> None:
        self._store.save(items)

    def add(self, text: str, *, conversation_id: Optional[str] = None,
            max_steps: int = 20, now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        with self._locked():
            items = self._load()
            goal_id = max((int(g.get("id", 0)) for g in items), default=0) + 1
            goal = {
                "id": goal_id,
                "text": text,
                "status": "active",
                "conversation_id": conversation_id,
                "max_steps": int(max_steps),
                "steps": 0,
                "log": [],
                "created_ts": now,
                "updated_ts": now,
            }
            items.append(goal)
            self._save(items)
            return goal

    def all(self) -> list[dict]:
        return self._load()

    def get(self, goal_id: int) -> Optional[dict]:
        for goal in self._load():
            if int(goal.get("id", 0)) == int(goal_id):
                return goal
        return None

    def active(self) -> list[dict]:
        return [g for g in self._load() if g.get("status") == "active"]

    def update(self, goal_id: int, **fields) -> Optional[dict]:
        with self._locked():
            items = self._load()
            updated = None
            for goal in items:
                if int(goal.get("id", 0)) == int(goal_id):
                    goal.update(fields)
                    updated = goal
                    break
            if updated is not None:
                self._save(items)
            return updated

    def transition(self, goal_id: int, status: str, now: float) -> Optional[dict]:
        return self.update(goal_id, status=status, updated_ts=now)

    def record_step(self, goal_id: int, *, now: float, entry: dict,
                    status: Optional[str] = None) -> Optional[dict]:
        """Atomically record one step's outcome (steps+1, log, updated_ts, and an
        optional terminal status) in a single locked write.

        Returns the updated goal, or None if it is gone or no longer active — e.g.
        the owner cancelled it during the step. Folding the step record and the
        status flip into one lock (and refusing to write a non-active goal) keeps a
        cancel that lands mid-step from being overwritten with "done"/"blocked",
        so the owner can always preempt a goal.
        """
        with self._locked():
            items = self._load()
            for goal in items:
                if int(goal.get("id", 0)) == int(goal_id):
                    if goal.get("status") != "active":
                        return None
                    goal["steps"] = int(goal.get("steps", 0)) + 1
                    goal["log"] = goal.get("log", []) + [entry]
                    goal["updated_ts"] = now
                    if status:
                        goal["status"] = status
                    self._save(items)
                    return goal
            return None


def _gate(config, now: float, fetch: Optional[Callable]) -> tuple[bool, str]:
    """The shared clock-work leash: headroom on the real weekly usage, unparked guard."""
    from .leash import clock_work_allowed
    return clock_work_allowed(config, now, fetch)


def parse_verdict(text: str) -> dict:
    """A judge reply into ``{"status": done|blocked|continue, "summary": ...}``.

    Scans for the first line that *starts with* one of the three tokens (so leading
    prose before the verdict line is tolerated, while a "not done" mid-sentence
    never trips a false DONE). A reply with no recognizable verdict fails OPEN to
    ``blocked`` — the judge did not actually rule, so the tick asks the owner rather
    than looping silently or claiming success.
    """
    for raw in (text or "").splitlines():
        line = raw.strip()
        upper = line.upper()
        for status in ("done", "blocked", "continue"):
            token = status.upper()
            rest = upper[len(token):] if upper.startswith(token) else None
            if rest is not None and not (rest and rest[0].isalpha()):
                # whole-word match, so 'DONENESS'/'CONTINUED' don't read as a verdict
                summary = line.split(":", 1)[1].strip() if ":" in line else ""
                return {"status": status, "summary": summary}
    return {"status": "blocked", "summary": "couldn't read a verdict from the judge"}


def _default_step(config):
    from .agent import Agent
    agent = Agent.from_config(config, clock_gated=True)

    def step(goal: dict) -> str:
        prompt = STEP_PROMPT.format(
            text=goal["text"], n=int(goal.get("steps", 0)) + 1,
            max=goal.get("max_steps", "?"),
        )
        result = agent.respond(f"goal:{goal['id']}", prompt)
        if result.is_error:
            raise RuntimeError(result.error or "goal step failed")
        return (result.text or "").strip()

    return step


def parse_confirmation(text: str) -> dict:
    """A verifier reply into ``{"confirmed": bool, "note": str}``.

    Conservative: only an explicit CONFIRMED counts as confirmed; an unreadable or
    missing verdict is treated as NOT confirmed, so an unverifiable "done" never
    slips through on a garbled check. UNCONFIRMED is matched first so it is never
    mistaken for the CONFIRMED substring it contains.
    """
    for raw in (text or "").splitlines():
        line = raw.strip()
        upper = line.upper()
        if upper.startswith("UNCONFIRMED"):
            note = line.split(":", 1)[1].strip() if ":" in line else ""
            return {"confirmed": False, "note": note}
        if upper.startswith("CONFIRMED"):
            note = line.split(":", 1)[1].strip() if ":" in line else ""
            return {"confirmed": True, "note": note}
    return {"confirmed": False, "note": "verifier gave no clear verdict"}


def _default_verify(config):
    from .agent import Agent
    agent = Agent.from_config(config, clock_gated=True)

    def verify(goal: dict, step_text: str) -> dict:
        # A fresh, independent session on the cheap model with the chat toolset, so
        # it can actually read the wiki/memory/web to check the claim. Prompted to
        # only read; runs on its own conversation id so it shares no worker context.
        result = agent.respond(
            f"goal-verify:{goal['id']}",
            VERIFY_PROMPT.format(text=goal["text"], step=step_text),
            model=(config.goal_judge_model or None),
        )
        if result.is_error:
            raise RuntimeError(result.error or "goal verify failed")
        return parse_confirmation(result.text)

    return verify


def _default_judge(config):
    from .driver import ClaudeDriver

    def judge(goal: dict, step_text: str) -> dict:
        driver = ClaudeDriver(
            claude_bin=config.claude_bin,
            model=config.goal_judge_model or "claude-haiku-4-5",
            # No mcp_config and the default built-in denylist: the judge reads the
            # report and rules on it, it does not act, so it gets no tools.
            restrict_builtin_tools=True,
            timeout=config.turn_timeout,
        )
        prompt = JUDGE_PROMPT.format(text=goal["text"], step=step_text)
        result = driver.run(prompt)
        if result.is_error:
            raise RuntimeError(result.error or "goal judge failed")
        return parse_verdict(result.text)

    return judge


def run_goal_tick(config, *, now: float, store: Optional[GoalStore] = None,
                  step: Optional[Callable] = None, judge: Optional[Callable] = None,
                  sender: Optional[Callable] = None, verify: Optional[Callable] = None,
                  fetch: Optional[Callable] = None) -> str:
    """Advance one active goal by a single step. Returns a one-word-ish status.

    Gated exactly like a proactive review (off unless IRIS_GOALS; the credit guard
    unparked; real weekly usage under the threshold) so it never crowds out the
    owner's own work. Then it picks the least-recently-worked active goal, runs one
    step (a model turn on the goal's own session), and has an independent judge rule
    on the result. The seams (step, judge, sender, fetch) are injected by tests.
    """
    if not getattr(config, "goals_enabled", False):
        return "disabled"

    store = store or GoalStore(config.goals_file)
    active = store.active()
    if not active:
        # Cheap path first only matters for spend, and the gate's model call is
        # the cache read; but skip the usage fetch entirely when there's no work.
        return "idle"

    allowed, detail = _gate(config, now, fetch)
    if not allowed:
        return f"skipped({detail})"

    # Least-recently-worked first: smallest updated_ts gets the clock, so many
    # goals rotate fairly and one slow goal cannot starve the others.
    goal = min(active, key=lambda g: g.get("updated_ts", g.get("created_ts", 0)))
    goal_id = goal["id"]
    max_steps = int(goal.get("max_steps", getattr(config, "goals_max_steps", 20)))
    done_steps = int(goal.get("steps", 0))

    def report(text: str) -> None:
        from .driver import _origin_channel
        channel = _origin_channel(goal.get("conversation_id")) or config.home_channel
        if not channel:
            return
        send = sender or _default_sender
        send(channel, text, config.discord_token)

    if done_steps >= max_steps:
        store.transition(goal_id, "blocked", now)
        report(f"[goal needs you] I've used my {max_steps}-step budget on “{goal['text']}” "
               "without finishing. Want me to extend it, change the approach, or drop it?")
        return "budget"

    step_fn = step or _default_step(config)
    try:
        step_text = step_fn(goal)
    except Exception as exc:
        # A failed model turn (rate limit, dead session) is transient: don't burn
        # the goal's budget on it, just try again on the next tick.
        log.warning("goal %s step failed: %s", goal_id, exc)
        return "step-error"

    judge_fn = judge or _default_judge(config)
    try:
        verdict = judge_fn(goal, step_text)
    except Exception as exc:
        # Fail-open: an unreachable judge must not silently loop or claim done.
        log.warning("goal %s judge failed: %s", goal_id, exc)
        verdict = {"status": "blocked", "summary": "couldn't verify progress this step"}

    status = (verdict or {}).get("status", "continue")
    summary = (verdict or {}).get("summary", "")

    # Independent verification of a "done" claim: the judge ruled on the worker's
    # self-report, so before accepting completion an independent read-only turn
    # checks the actual work. Fires ONLY on done (bounded cost). An unconfirmed or
    # erroring verify fails open to "ask the owner" rather than completing blind.
    if status == "done" and getattr(config, "goals_verify_done", True):
        verify_fn = verify or _default_verify(config)
        try:
            confirmation = verify_fn(goal, step_text)
        except Exception as exc:
            log.warning("goal %s verify failed: %s", goal_id, exc)
            confirmation = {"confirmed": False, "note": "independent verification was unavailable"}
        if not confirmation.get("confirmed"):
            status = "blocked"
            summary = ("reported done, but independent verification did not confirm it: "
                       + (confirmation.get("note") or "")).strip()

    entry = {"ts": now, "step": (step_text or "")[:600], "status": status, "summary": summary}
    terminal = status if status in ("done", "blocked") else None
    recorded = store.record_step(goal_id, now=now, entry=entry, status=terminal)
    if recorded is None:
        # The goal was cancelled (or otherwise left active) during the step;
        # don't clobber that or report on a goal the owner just dropped.
        return "cancelled"

    if status == "done":
        report(f"[goal done] “{goal['text']}”: {summary or step_text}".strip())
        return "done"
    if status == "blocked":
        report(f"[goal needs you] on “{goal['text']}”: {summary or step_text}".strip())
        return "blocked"
    return "advanced"


def _default_sender(channel: str, text: str, token: str) -> bool:
    from .reminders import send_discord_message
    return send_discord_message(channel, text, token)
