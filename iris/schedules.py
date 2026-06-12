"""Owner-authored scheduled jobs: the one place the clock may start work.

This is a deliberate, owner-decided relaxation of the original
zero-idle-inference invariant (2026-06-12). The new line is:

    The clock may start a pre-recorded, owner-authored job. It may never
    start a conversation, a decision, or anything the owner didn't write
    down.

Everything around that line is load-bearing:

* **Default-off.** ``IRIS_SCHEDULED_JOBS`` gates the whole tick, separately
  from ``IRIS_JOBS``.
* **Owner-authored.** A rule's instructions (or shell command) are recorded
  verbatim when the rule is created; the tick composes nothing.
* **The gated launch path.** A job rule fires through the console's
  ``gated_launch``: grants re-clamped to the current ceiling, the credit
  guard parks it when the month runs hot, ``jobs_max`` still admits.
* **Caps.** Every rule carries a monthly fire cap, and a rule whose previous
  job is still pending/running/parked skips its firing instead of stacking.
* **Script mode.** A rule with a ``command`` instead of ``instructions``
  spawns a detached ``iris watch`` run: zero model calls on the happy path,
  the notify spine's usual ping when it fails or runs long.

Rules live in their own store, NOT in the reminders file: the reminders MCP
tool lets the model write reminders, and nothing the model can write may ever
carry a launch payload. Rule creation goes through the owner's CLI
(``iris schedule``) or the explicitly allowlisted jobs-server tool.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .config import Config
from .reminders import parse_every, parse_when
from .statefile import quarantine_corrupt
from .usage import month_key

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

log = logging.getLogger("iris.schedules")

# A previous firing in any of these states blocks the next one: parked counts,
# or a hot month would stack one parked clone per tick interval.
_BLOCKING_STATES = ("pending", "running", "parked")


class ScheduleStore:
    """The schedule registry: a JSON list with a cross-process lock."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None:
            yield
            return
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        with open(lock, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text("utf-8"))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, OSError):
            quarantine_corrupt(self.path, "schedule registry")
            return []

    def _save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(items, handle, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def add(self, **fields) -> dict:
        with self._locked():
            items = self._load()
            rule = {"id": max((int(i.get("id", 0)) for i in items), default=0) + 1}
            rule.update(fields)
            items.append(rule)
            self._save(items)
            return dict(rule)

    def all(self) -> list[dict]:
        return sorted(self._load(), key=lambda r: r.get("id", 0))

    def get(self, rule_id: int) -> Optional[dict]:
        for rule in self._load():
            if rule.get("id") == rule_id:
                return rule
        return None

    def remove(self, rule_id: int) -> bool:
        with self._locked():
            items = self._load()
            kept = [r for r in items if r.get("id") != rule_id]
            if len(kept) == len(items):
                return False
            self._save(kept)
            return True

    def update(self, rule_id: int, **fields) -> Optional[dict]:
        with self._locked():
            items = self._load()
            for rule in items:
                if rule.get("id") == rule_id:
                    rule.update(fields)
                    self._save(items)
                    return dict(rule)
            return None

    def take_due(self, now: float) -> list[dict]:
        """Atomically advance every due, enabled rule and return what to fire.

        A recurring rule's next due time is set forward from ``now`` (one
        firing per missed window, like reminders); a one-shot rule is disabled
        in place so it stays visible in ``iris schedule list``.
        """
        with self._locked():
            items = self._load()
            due: list[dict] = []
            for rule in items:
                if not rule.get("enabled", True):
                    continue
                if float(rule.get("due_ts", 0)) > now:
                    continue
                due.append(dict(rule))
                period = int(rule.get("repeat_secs", 0) or 0)
                if period > 0:
                    rule["due_ts"] = now + period
                else:
                    rule["enabled"] = False
            if due:
                self._save(items)
            return due


def add_rule(store: ScheduleStore, *, title: str, when: str, every: str = "",
             instructions: str = "", command: str = "", grants: str = "",
             workspace: str = "", cap: Optional[int] = None,
             created_by: str = "owner", default_cap: int = 62,
             now: Optional[float] = None) -> dict:
    """Validate and record one schedule rule. Raises ValueError on bad input.

    Exactly one of ``instructions`` (a job rule) or ``command`` (a script
    rule) must be given. Grants are validated here and clamped again at every
    launch, so a ceiling lowered after the rule was written still applies.
    """
    from .jobs import parse_grants

    title = (title or "").strip()
    instructions = (instructions or "").strip()
    command = (command or "").strip()
    if not title:
        raise ValueError("a schedule needs a title")
    if bool(instructions) == bool(command):
        raise ValueError("give exactly one of instructions (a job) or command (a script)")
    due = parse_when(when, now)
    repeat_secs = parse_every(every or "")
    granted_names = parse_grants(grants)
    if cap is None:
        cap = default_cap
    cap = int(cap)
    if cap <= 0:
        raise ValueError("the monthly cap must be a positive count")
    return store.add(
        title=title,
        instructions=instructions,
        command=command,
        grants=granted_names,
        workspace=(workspace or "").strip(),
        due_ts=due,
        repeat_secs=repeat_secs,
        monthly_cap=cap,
        fired={},
        last_job_id=None,
        enabled=True,
        created_by=created_by,
        created_ts=time.time() if now is None else now,
    )


def describe_rule(rule: dict) -> str:
    """One line for `iris schedule list` and the MCP listing."""
    from .reminders import fmt_ts

    period = int(rule.get("repeat_secs", 0) or 0)
    cadence = f" every {period // 86400}d" if period and period % 86400 == 0 else (
        f" every {period // 3600}h" if period and period % 3600 == 0 else (
            f" every {period // 60}m" if period else " once"))
    mode = "job" if rule.get("instructions") else "script"
    state = "" if rule.get("enabled", True) else " [disabled]"
    fired = sum(int(v) for v in (rule.get("fired") or {}).values())
    return (f"#{rule['id']} [{mode}]{state} {rule['title']} — next {fmt_ts(rule.get('due_ts', 0))}"
            f"{cadence}, cap {rule.get('monthly_cap')}/month, fired {fired} this month")


def _fire_job_rule(config: Config, rule: dict, spawn) -> tuple[bool, str, Optional[int]]:
    """Launch one job rule through the single gated path. (fired?, note, job id)."""
    from .jobs import JobStore, spawn_runner
    from .jobs_console import gated_launch

    if not config.jobs_enabled:
        return False, f"#{rule['id']} skipped: jobs are disabled (set IRIS_JOBS=true)", None
    jstore = JobStore(config.jobs_file, keep=config.jobs_keep)
    last = rule.get("last_job_id")
    if isinstance(last, int):
        prior = jstore.get(last)
        if prior and prior.get("state") in _BLOCKING_STATES:
            return False, (f"#{rule['id']} skipped: the previous run (job #{last}) "
                           f"is still {prior['state']} (no overlap)"), None
    result = gated_launch(
        config, jstore,
        title=f"[scheduled] {rule['title']}",
        instructions=rule.get("instructions", ""),
        grants=list(rule.get("grants") or []),
        workspace=rule.get("workspace", ""),
        spawn=spawn or spawn_runner,
    )
    job = result["job"]
    return True, f"#{rule['id']} {result['outcome']} job #{job['id']}", job["id"]


def _fire_script_rule(rule: dict, popen) -> tuple[bool, str, Optional[int]]:
    """Spawn a detached `iris watch` run for a script rule. Zero model calls."""
    popen = popen or subprocess.Popen
    popen(
        [sys.executable, "-m", "iris", "watch",
         "--name", f"[scheduled] {rule['title']}",
         "--", "/bin/sh", "-c", rule.get("command", "")],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return True, f"#{rule['id']} launched script", None


def tick_schedules(config: Config, now: Optional[float] = None,
                   spawn=None, popen=None) -> str:
    """Fire due schedule rules. Runs inside reminders-tick, fail-soft.

    Gated on IRIS_SCHEDULED_JOBS; with it unset this returns immediately and
    the tick stays a pure REST delivery loop, exactly as before.
    """
    if not config.scheduled_jobs_enabled:
        return "schedules: off"
    now = time.time() if now is None else now
    store = ScheduleStore(config.schedules_file)
    due = store.take_due(now)
    mkey = month_key(now)
    launched = 0
    notes: list[str] = []
    for rule in due:
        fired_map = {k: int(v) for k, v in (rule.get("fired") or {}).items() if k == mkey}
        count = fired_map.get(mkey, 0)
        cap = int(rule.get("monthly_cap", 0) or 0)
        if cap and count >= cap:
            notes.append(f"#{rule['id']} skipped: at its monthly cap ({cap})")
            continue
        try:
            if rule.get("instructions"):
                fired, note, job_id = _fire_job_rule(config, rule, spawn)
            else:
                fired, note, job_id = _fire_script_rule(rule, popen)
        except Exception as exc:  # one broken rule must not stall the rest
            log.exception("schedule rule %s failed to fire", rule.get("id"))
            fired, note, job_id = False, f"#{rule.get('id')} failed to fire: {exc}", None
        notes.append(note)
        if fired:
            launched += 1
            updates: dict = {"fired": {mkey: count + 1}}
            if job_id is not None:
                updates["last_job_id"] = job_id
            store.update(rule["id"], **updates)
    summary = f"schedules: {len(due)} due, {launched} launched"
    if notes:
        summary += " (" + "; ".join(notes) + ")"
    return summary
