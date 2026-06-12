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
* **Caps.** Every rule carries a monthly fire cap that only actual starts
  consume, and overlap is guarded both ways: a job rule skips while its
  previous job is still running (a stale parked/queued clone is cancelled and
  replaced instead of wedging the rule), and a script rule skips while its
  previous process is still alive.
* **Script mode.** A rule with a ``command`` instead of ``instructions``
  spawns a detached ``iris watch`` run: zero model calls on the happy path,
  and the failure-triage call honors the credit-guard park level.

Rules live in their own store, NOT in the reminders file: the reminders MCP
tool lets the model write reminders, and a reminder must never carry a launch
payload. Rule creation goes through the owner's CLI (``iris schedule``), or —
for job rules only, capped, and only when the owner allowlists it — the
jobs-server ``schedule_job`` tool. The model can never put a shell command on
the clock.
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
        except (json.JSONDecodeError, OSError):
            quarantine_corrupt(self.path, "schedule registry")
            return []
        if not isinstance(data, list):
            # Valid JSON of the wrong shape (a hand edit) is still owner data;
            # quarantine it rather than letting the next save overwrite it.
            quarantine_corrupt(self.path, "schedule registry")
            return []
        return data

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
                if isinstance(rule, dict) and rule.get("id") == rule_id:
                    rule.update(fields)
                    self._save(items)
                    return dict(rule)
            return None

    def update_if(self, rule_id: int, expected_created_ts, **fields) -> Optional[dict]:
        """``update`` guarded by identity, not just id.

        Ids can be reused after a remove, so the tick stamps fire results back
        only onto the exact rule it snapshotted — a rule recreated mid-firing
        must not inherit the old rule's fire count or last job.
        """
        with self._locked():
            items = self._load()
            for rule in items:
                if isinstance(rule, dict) and rule.get("id") == rule_id:
                    if rule.get("created_ts") != expected_created_ts:
                        return None
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
                # Malformed entries (a hand edit gone wrong) are skipped in
                # place, never fired and never destroyed, so one bad rule
                # cannot stall the rest of the store.
                if not isinstance(rule, dict):
                    continue
                if not rule.get("enabled", True):
                    continue
                due_ts = rule.get("due_ts")
                if not isinstance(due_ts, (int, float)) or due_ts > now:
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


def describe_rule(rule: dict, now: Optional[float] = None) -> str:
    """One line for `iris schedule list` and the MCP listing."""
    from .reminders import fmt_ts

    period = int(rule.get("repeat_secs", 0) or 0)
    cadence = f" every {period // 86400}d" if period and period % 86400 == 0 else (
        f" every {period // 3600}h" if period and period % 3600 == 0 else (
            f" every {period // 60}m" if period else " once"))
    mode = "job" if rule.get("instructions") else "script"
    state = "" if rule.get("enabled", True) else " [disabled]"
    # Old months are pruned only when a rule fires, so the listing must filter
    # to the current month itself or a May count reads as June's.
    try:
        fired = int((rule.get("fired") or {}).get(month_key(now), 0))
    except (TypeError, ValueError):
        fired = 0
    return (f"#{rule.get('id')} [{mode}]{state} {rule.get('title', '(untitled)')} — next {fmt_ts(rule.get('due_ts', 0))}"
            f"{cadence}, cap {rule.get('monthly_cap')}/month, fired {fired} this month")


def _fire_job_rule(config: Config, rule: dict, spawn) -> dict:
    """Launch one job rule through the single gated path.

    Returns ``{"started": bool, "note": str, "updates": dict}``: ``started``
    is True only when the runner actually spawned (parked and queued firings
    burn no model call, so they must not burn a cap slot either); ``updates``
    is what the tick writes back onto the rule.
    """
    from .inbox import Inbox
    from .jobs import JobStore, repair_dead_runners, spawn_runner
    from .jobs_console import gated_launch

    rid = rule["id"]
    if not config.jobs_enabled:
        return {"started": False, "recorded": False, "updates": {},
                "note": f"#{rid} skipped: jobs are disabled (set IRIS_JOBS=true)"}
    jstore = JobStore(config.jobs_file, keep=config.jobs_keep)
    # On a schedule-only deployment the tick may be the ONLY thing that ever
    # touches the job store, so it must do its own dead-runner repair or a
    # runner lost to a reboot leaves the rule bricked behind the overlap check.
    repair_dead_runners(jstore)
    last = rule.get("last_job_id")
    if isinstance(last, int):
        prior = jstore.get(last)
        if prior and prior.get("state") == "running":
            return {"started": False, "recorded": False, "updates": {},
                    "note": f"#{rid} skipped: the previous run (job #{last}) "
                            f"is still running (no overlap)"}
        if prior and prior.get("state") in ("parked", "pending"):
            # Nothing auto-resumes a parked or queued job, so a hot month or a
            # full jobs_max minute must not wedge the rule forever. Cancel the
            # stale clone and launch fresh: if the guard is still hot the new
            # launch parks again, so there is at most one clone at a time.
            jstore.transition(last, ("parked", "pending"), "cancelled",
                              finished_ts=time.time(),
                              error="superseded by the schedule's next firing")
    result = gated_launch(
        config, jstore,
        title=f"[scheduled] {rule['title']}",
        instructions=rule.get("instructions", ""),
        grants=list(rule.get("grants") or []),
        workspace=rule.get("workspace", ""),
        spawn=spawn or spawn_runner,
    )
    job, outcome = result["job"], result["outcome"]
    if outcome in ("parked", "queued"):
        # The tick's stdout usually goes to cron's bit bucket; the fold-back
        # inbox is how the owner actually hears that a schedule is waiting.
        try:
            Inbox(config.inbox_file).append(
                f"scheduled rule #{rid} ({rule['title']}): firing was {outcome} "
                f"as job #{job['id']}; it will retry on the next firing, or "
                f"resume it now with resume_job({job['id']})."
            )
        except Exception:
            log.warning("could not write the inbox note for rule %s", rid, exc_info=True)
    # started/parked/queued all create a job row, so the rule has fired and a
    # one-shot may be consumed; a skip with no job recorded must not consume it.
    return {"started": outcome == "started", "recorded": True,
            "updates": {"last_job_id": job["id"]},
            "note": f"#{rid} {outcome} job #{job['id']}"}


def _fire_script_rule(rule: dict, popen) -> dict:
    """Spawn a detached `iris watch` run for a script rule. Zero model calls.

    The previous firing's pid gates the next one (best-effort: pids can in
    principle be recycled, but a false 'still running' costs one skipped
    firing, not a stack of concurrent commands).
    """
    from .jobs import _pid_alive

    rid = rule["id"]
    last_pid = rule.get("last_script_pid")
    if isinstance(last_pid, int) and last_pid > 0 and _pid_alive(last_pid):
        return {"started": False, "recorded": False, "updates": {},
                "note": f"#{rid} skipped: the previous script run (pid {last_pid}) "
                        f"is still running (no overlap)"}
    popen = popen or subprocess.Popen
    proc = popen(
        [sys.executable, "-m", "iris", "watch",
         "--name", f"[scheduled] {rule['title']}",
         "--", "/bin/sh", "-c", rule.get("command", "")],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid = getattr(proc, "pid", None)
    updates = {"last_script_pid": pid} if isinstance(pid, int) else {}
    return {"started": True, "recorded": True, "updates": updates,
            "note": f"#{rid} launched script"}


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
        # The whole per-rule body is guarded: one rule with a corrupt field
        # must not abort the batch after take_due already consumed it.
        try:
            count = int((rule.get("fired") or {}).get(mkey, 0))
            cap = int(rule.get("monthly_cap", 0) or 0)
            if cap and count >= cap:
                notes.append(f"#{rule['id']} skipped: at its monthly cap ({cap})")
                continue
            if rule.get("instructions"):
                outcome = _fire_job_rule(config, rule, spawn)
            else:
                outcome = _fire_script_rule(rule, popen)
            notes.append(outcome["note"])
            updates = dict(outcome["updates"])
            if outcome["started"]:
                launched += 1
                # Replacing the whole map prunes old months' counts.
                updates["fired"] = {mkey: count + 1}
            # take_due disabled a one-shot rule when it came due; if nothing was
            # actually recorded (jobs off, a prior run still active), it never
            # ran, so re-enable it to retry instead of silently losing it.
            if not outcome.get("recorded", outcome["started"]) and \
                    int(rule.get("repeat_secs", 0) or 0) == 0:
                updates["enabled"] = True
            if updates:
                # Guarded by created_ts: the rule may have been removed and
                # its id reused while this firing was in flight.
                store.update_if(rule["id"], rule.get("created_ts"), **updates)
        except Exception as exc:  # one broken rule must not stall the rest
            log.exception("schedule rule %s failed to fire", rule.get("id"))
            notes.append(f"#{rule.get('id')} failed to fire: {exc}")
    summary = f"schedules: {len(due)} due, {launched} launched"
    if notes:
        summary += " (" + "; ".join(notes) + ")"
    return summary
