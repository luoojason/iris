"""Background job coordination: registry, per-job driver policy, and runner.

JobStore is a file-backed registry shaped like ReminderStore (fcntl sidecar
lock, atomic tempfile+os.replace writes, corrupt-tolerant load) so the MCP
jobs server subprocess and the bot process can share it safely, and every
state change hits disk before returning. build_job_driver derives a per-job
ClaudeDriver from the chat driver without ever mutating it. JobRunner is the
bot-side lifecycle owner: it claims pending jobs, runs each through a per-job
StreamDriver, persists outcomes, and delivers (fold-back first, notify spine
fallback).

Faking seams: stores take a path (tests use tmp_path); the drivers built here
are pure dataclasses whose build_command output is asserted directly; the
runner takes stream_driver_factory / deliver / sender / notify_driver_factory
fakes and a sync=True inline mode, and exposes workers + turn_registered as
joinable handles.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Sequence

from . import budget
from .driver import (
    DANGEROUS_BUILTINS,
    ClaudeDriver,
    ClaudeResult,
    is_credit_or_rate_pushback,
)
from .metrics import emit_turn
from .notify.compose import render
from .notify.deliver import send as _notify_send
from .notify.events import Event
from .notify.gate import decide, needs_model
from .stream_driver import StreamDriver

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

log = logging.getLogger("iris.jobs")

# Appended on top of the chat persona so a job knows it is not the live
# conversation: no one answers questions mid-run, and the closing message is
# all the owner ever sees of the work.
JOB_PREAMBLE = (
    "You are a background worker spawned by Iris to handle one job autonomously. "
    "No one is watching the run and no one will answer questions, so make "
    "reasonable decisions yourself and see the job through. Your final message "
    "is the report delivered to the owner: state what you did, what you found, "
    "and anything left undone."
)


class JobStore:
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
            return []

    def _save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(items, handle, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def add(
        self,
        prompt: str,
        title: str,
        *,
        model: str = "",
        timeout_s: Optional[int] = None,
        grants: Optional[list[str]] = None,
        channel_id: str = "",
        conversation_id: str = "",
    ) -> int:
        with self._locked():
            items = self._load()
            new_id = max((int(i.get("id", 0)) for i in items), default=0) + 1
            items.append({
                "id": new_id,
                "title": title,
                "prompt": prompt,
                "status": "pending",
                "created_at": time.time(),
                "started_at": None,
                "finished_at": None,
                "channel_id": channel_id,
                "conversation_id": conversation_id,
                "model": model,
                "timeout_s": int(timeout_s) if timeout_s else 1800,
                "grants": list(grants or []),
                "cancel_requested": False,
                "result": None,
            })
            self._save(items)
        return new_id

    def update(self, job_id: int, **fields) -> bool:
        with self._locked():
            items = self._load()
            for job in items:
                if job.get("id") == job_id:
                    job.update(fields)
                    self._save(items)
                    return True
            return False

    def all(self, status: Optional[str] = None) -> list[dict]:
        jobs = sorted(self._load(), key=lambda i: i.get("id", 0))
        if status:
            jobs = [j for j in jobs if j.get("status") == status]
        return jobs

    def get(self, job_id: int) -> Optional[dict]:
        for job in self._load():
            if job.get("id") == job_id:
                return job
        return None

    def claim_pending(self, limit: int, now: Optional[float] = None) -> list[dict]:
        """Atomically flip up to ``limit`` pending jobs to running and return them.

        The pop_due analog: the flip and the started_at stamp are saved under
        the lock before returning, so two claimers can never run the same job.
        Oldest (lowest id) first.
        """
        if limit <= 0:
            return []
        now = time.time() if now is None else now
        with self._locked():
            items = self._load()
            claimed = []
            for job in sorted(items, key=lambda i: i.get("id", 0)):
                if len(claimed) >= limit:
                    break
                if job.get("status") == "pending":
                    job["status"] = "running"
                    job["started_at"] = now
                    claimed.append(job)
            if claimed:
                self._save(items)
            return claimed

    def request_cancel(self, job_id: int) -> str:
        """Cancel a pending job outright; flag a running one for its runner.

        Friendly strings only (this feeds straight back to the model/CLI). A
        running job is not flipped here: the runner owns the process handle, so
        it kills the turn and records ``cancelled`` itself.
        """
        with self._locked():
            items = self._load()
            for job in items:
                if job.get("id") != job_id:
                    continue
                status = job.get("status")
                if status == "pending":
                    job["status"] = "cancelled"
                    self._save(items)
                    return f"Cancelled job #{job_id}."
                if status == "running":
                    job["cancel_requested"] = True
                    self._save(items)
                    return f"Asked the runner to stop job #{job_id}."
                return f"Job #{job_id} already finished."
            return f"No job #{job_id}."


# The subagent tool answers to two names: Claude Code 2.1.63 renamed Task to
# Agent and both still resolve, so a grant or ceiling naming either must cover
# both or the leftover alias stays live.
_SUBAGENT_NAMES = frozenset({"Task", "Agent"})

# The jobs MCP server's permission-rule name; "mcp__jobs" alone denies every
# tool the server exposes, present or future.
_JOBS_SERVER = "mcp__jobs"


def _expand_subagent_alias(names: Sequence[str]) -> set[str]:
    expanded = set(names)
    if expanded & _SUBAGENT_NAMES:
        expanded |= _SUBAGENT_NAMES
    return expanded


def _is_jobs_tool(name: str) -> bool:
    return name == _JOBS_SERVER or name.startswith(_JOBS_SERVER + "__")


def build_job_driver(
    base_driver: ClaudeDriver,
    job: dict,
    *,
    grant_ceiling: Sequence[str],
) -> ClaudeDriver:
    """Derive the driver for one background job from the chat driver.

    The chat driver is never mutated: dataclasses.replace builds a copy with
    the per-job timeout, model override, and the job preamble. The denylist is
    COMPUTED from DANGEROUS_BUILTINS minus the granted tools, because an
    explicit ``disallowed_tools`` replaces the default denylist entirely
    (driver._effective_disallowed); hand-listing it would silently drift.
    Granted = the job's requested grants intersected with the operator ceiling,
    so a job can never talk itself into more reach than IRIS_JOB_GRANTS allows.

    The jobs tools themselves never reach a worker: the chat allowlist carries
    over, but a worker holding mcp__jobs__spawn_job could be prompt-injected
    (it runs unattended with web reach) into queueing descendants without
    bound. Internal fan-out is the Task grant, only: the jobs tools are
    stripped from the allowlist and the whole server is denied outright.
    """
    granted = _expand_subagent_alias(job.get("grants") or ()) & _expand_subagent_alias(
        grant_ceiling
    )
    allowed = base_driver.allowed_tools
    if allowed:
        allowed = tuple(t for t in allowed if not _is_jobs_tool(t))
    return dataclasses.replace(
        base_driver,
        timeout=float(job.get("timeout_s") or base_driver.timeout),
        model=job.get("model") or base_driver.model,
        append_system_prompt=JOB_PREAMBLE,
        allowed_tools=allowed,
        disallowed_tools=tuple(t for t in DANGEROUS_BUILTINS if t not in granted)
        + (_JOBS_SERVER,),
    )


class JobRunner:
    """Bot-side job lifecycle owner: claim pending jobs, run each one through a
    per-job StreamDriver on a worker thread, persist the outcome, and deliver.

    Test seams: ``stream_driver_factory`` replaces the real StreamDriver,
    ``sync=True`` runs workers inline and skips the watcher thread,
    ``deliver``/``sender`` capture both delivery paths, and ``workers`` (a
    joinable dict of threads) plus ``turn_registered`` (set whenever a worker
    registers its live turn) bound every test-side wait.
    """

    def __init__(
        self,
        store: JobStore,
        base_driver: ClaudeDriver,
        *,
        grant_ceiling: Sequence[str] = ("Task",),
        concurrency: int = 2,
        idle_timeout: float = 300.0,
        poll_seconds: float = 2.0,
        deliver: Optional[Callable[[str, str, str], bool]] = None,
        sender=None,
        notify_channel: str = "",
        discord_token: str = "",
        notify_driver_factory=None,
        watch_min_seconds: float = 30.0,
        metrics_path: str = "",
        budget_state_path: str = "",
        monthly_credit: float = 0.0,
        light_model: str = "",
        park_minutes: float = 60.0,
        stream_driver_factory=None,
        clock: Callable[[], float] = time.monotonic,
        sync: bool = False,
    ) -> None:
        self.store = store
        self.base_driver = base_driver
        self.grant_ceiling = tuple(grant_ceiling)
        self.concurrency = int(concurrency)
        self.idle_timeout = float(idle_timeout)
        self.poll_seconds = float(poll_seconds)
        self.deliver = deliver
        self.sender = sender
        self.notify_channel = notify_channel
        self.discord_token = discord_token
        self.notify_driver_factory = notify_driver_factory
        self.watch_min_seconds = float(watch_min_seconds)
        self.metrics_path = metrics_path
        # Credit guard: budget_state_path enables parking on credit/rate
        # pushback; monthly_credit + light_model enable near-cap tightening.
        self.budget_state_path = budget_state_path
        self.monthly_credit = float(monthly_credit)
        self.light_model = light_model
        self.park_minutes = float(park_minutes)
        self.stream_driver_factory = stream_driver_factory
        self.clock = clock
        self.sync = bool(sync)

        # Joinable handles for tests; workers are daemon threads keyed by job id.
        self.workers: dict[int, threading.Thread] = {}
        self.turn_registered = threading.Event()
        self.watcher: Optional[threading.Thread] = None

        self._turns: dict[int, object] = {}      # live StreamTurn per running job
        self._cancel_flagged: set[int] = set()   # ids whose turn we cancelled
        self._windows: dict[str, tuple[str, float]] = {}  # cid -> (channel, t0)
        self._ambiguous: set[int] = set()        # ids born under overlapping windows
        self._state_lock = threading.Lock()
        self._check_lock = threading.Lock()
        self._sem = threading.Semaphore(self.concurrency)
        self._stop_event = threading.Event()

    @classmethod
    def from_config(cls, config, base_driver: ClaudeDriver, *,
                    deliver: Optional[Callable[[str, str, str], bool]] = None,
                    sender=None) -> "JobRunner":
        """Build the runner the adapters use, mapped from Config's job fields.

        The chat driver is reused as the job base (its tool/mcp/persona wiring
        carries over); when ``job_model`` is set a replaced copy carries the
        override so the chat driver itself is never touched. The notify driver
        is built lazily per failure, exactly like ``iris watch``'s triage path.
        """
        from .notify.watch_cmd import build_notify_driver

        base = base_driver
        if config.job_model:
            base = dataclasses.replace(base_driver, model=config.job_model)
        return cls(
            JobStore(config.jobs_file),
            base,
            grant_ceiling=tuple(config.job_grants),
            concurrency=config.job_concurrency,
            idle_timeout=config.job_idle_timeout,
            poll_seconds=config.job_poll_seconds,
            deliver=deliver,
            sender=sender,
            notify_channel=config.notify_channel,
            discord_token=config.discord_token,
            notify_driver_factory=lambda: build_notify_driver(config),
            watch_min_seconds=config.watch_min_seconds,
            metrics_path=config.metrics_file,
            budget_state_path=config.budget_state,
            monthly_credit=config.monthly_credit,
            light_model=config.light_model,
            park_minutes=config.budget_park_minutes,
        )

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        """Recover orphans, then watch the registry file (skipped when sync)."""
        self._recover_interrupted()
        if self.sync:
            return
        self._stop_event.clear()
        self.watcher = threading.Thread(target=self._watch_loop,
                                        name="job-watcher", daemon=True)
        self.watcher.start()

    def stop(self) -> None:
        self._stop_event.set()
        watcher = self.watcher
        if watcher is not None and watcher.is_alive():
            watcher.join(timeout=2.0)

    def _recover_interrupted(self) -> None:
        # A job stored as running with no live handle here was killed mid-run
        # by a restart. Template-only forced ping: recovery happens on a clock
        # (process start), so it must never spend a model call.
        for job in self.store.all(status="running"):
            jid = int(job["id"])
            with self._state_lock:
                if jid in self._turns:
                    continue
            self.store.update(jid, status="interrupted")
            started_at = job.get("started_at") or time.time()
            self._spine_notify(
                kind="interrupted",
                title=job.get("title") or f"job #{jid}",
                exit_code=1,
                duration_s=max(0.0, time.time() - started_at),
                tail="",
                channel=job.get("channel_id") or "",
                allow_model=False,
            )

    def _watch_loop(self) -> None:
        # Pure file I/O on the clock: stat the registry and re-read only when
        # its mtime moved. The None baseline makes the first sighting of the
        # file count as a change, so jobs queued while the bot was down are
        # picked up within one poll.
        last_mtime = None
        while not self._stop_event.wait(self.poll_seconds):
            try:
                mtime = os.stat(self.store.path).st_mtime
            except OSError:
                continue
            if mtime != last_mtime:
                last_mtime = mtime
                try:
                    self.check_now()
                except Exception:
                    # The watcher is the jobs system's heartbeat: one transient
                    # registry error (disk full, permissions) must not kill the
                    # thread and silently stop all claiming until restart.
                    log.warning("job check failed; watcher continues", exc_info=True)

    # -- attribution -----------------------------------------------------------

    def turn_started(self, conversation_id: str, channel_id: str = "") -> None:
        """Open a stamping window: a chat turn for this conversation is live.

        Wall-clock (time.time()) on purpose: window edges are compared against
        JobStore's created_at, which is wall-clock too.
        """
        with self._state_lock:
            self._windows[conversation_id] = (channel_id, time.time())

    def turn_finished(self, conversation_id: str) -> None:
        """Close the window, stamp jobs born inside exactly it, nudge a check.

        MCP tools cannot see their calling conversation, so attribution is by
        time: an unstamped job whose created_at falls inside exactly one
        active window belongs to that conversation. A job inside two windows
        is ambiguous forever (remembered), so a later turn_finished cannot
        adopt it once the competing window is gone.
        """
        closed_at = time.time()
        with self._state_lock:
            window = self._windows.pop(conversation_id, None)
            other_starts = [t0 for (_, t0) in self._windows.values()]
        try:
            if window is not None:
                channel_id, opened_at = window
                for job in self.store.all():
                    if job.get("conversation_id"):
                        continue
                    jid = int(job["id"])
                    created = job.get("created_at") or 0.0
                    if not (opened_at <= created <= closed_at):
                        continue
                    with self._state_lock:
                        ambiguous = jid in self._ambiguous
                    # Any other still-open window that opened before the job was
                    # created also contains it: overlapping claims, stamp nobody.
                    if ambiguous or any(created >= t0 for t0 in other_starts):
                        with self._state_lock:
                            self._ambiguous.add(jid)
                        continue
                    self.store.update(jid, conversation_id=conversation_id,
                                      channel_id=channel_id)
            self.check_now()
        except Exception:
            # Stamping and the claim nudge ride the chat turn's exit path (the
            # adapter brackets), so a registry I/O failure here must never
            # sink the reply the model call already paid for.
            log.warning("job stamping/check after turn %s failed",
                        conversation_id, exc_info=True)

    # -- discovery -----------------------------------------------------------

    def check_now(self) -> None:
        """Honor cancel requests on owned turns, then claim into the free slots.

        While a budget park is live no claim happens: pending jobs stay
        queued and the watcher keeps polling, so expiry is noticed on the
        next file change or nudge.
        """
        with self._check_lock:
            self._cancel_pass()
            claimed = [] if self._parked() else self._claim_pass()
        for job in claimed:
            if self.sync:
                self._worker(job)
            else:
                thread = threading.Thread(
                    target=self._worker, args=(job,),
                    name=f"job-worker:{job['id']}", daemon=True,
                )
                self.workers[int(job["id"])] = thread
                thread.start()

    def _cancel_pass(self) -> None:
        # Only the runner owns process handles, so request_cancel left the
        # record running with cancel_requested set; kill the turn here and let
        # the worker (the single status writer) record the final state. An ok
        # primary that landed before the kill still wins as "done".
        with self._state_lock:
            owned = dict(self._turns)
        for jid, turn in owned.items():
            record = self.store.get(jid)
            if not record or record.get("status") != "running":
                continue
            if not record.get("cancel_requested"):
                continue
            with self._state_lock:
                self._cancel_flagged.add(jid)
            turn.cancel()

    def _parked(self) -> bool:
        """True while a budget park is live; an expired park clears with one ping.

        Wall-clock, like BudgetState's stored epoch. The reminders tick may
        clear an expired park first (same state file); then no ping is owed.
        """
        if not self.budget_state_path:
            return False
        state = budget.BudgetState(self.budget_state_path)
        until = state.park_until
        if until <= 0:
            return False
        if time.time() < until:
            return True
        state.set_park_until(0.0)
        self._budget_ping("jobs resumed: the budget park expired")
        return False

    def _claim_pass(self) -> list[dict]:
        # Count the free slots by draining the semaphore non-blocking; permits
        # for jobs we failed to claim go straight back, even when the claim
        # itself raises: a leaked permit is gone for the life of the process.
        # Each claimed job keeps its permit until its worker releases it.
        free = 0
        while free < self.concurrency and self._sem.acquire(blocking=False):
            free += 1
        if not free:
            return []
        claimed: list[dict] = []
        try:
            claimed = self.store.claim_pending(free)
        finally:
            for _ in range(free - len(claimed)):
                self._sem.release()
        return claimed

    # -- worker ---------------------------------------------------------------

    def _worker(self, job: dict) -> None:
        jid = int(job["id"])
        try:
            self._run_job(jid, job)
        except Exception as exc:
            # A worker must never die without recording an outcome: a job
            # stuck "running" forever lies to list_jobs and can never be
            # cancelled (the cancel pass only sees live turns).
            log.warning("job %s worker crashed", jid, exc_info=True)
            try:
                self._finish_job(jid, job, ClaudeResult(
                    text="", session_id=None, is_error=True,
                    error=f"job worker crashed: {exc}",
                ))
            except Exception:
                log.warning("job %s outcome write failed", jid, exc_info=True)
        finally:
            with self._state_lock:
                self._turns.pop(jid, None)
                self._cancel_flagged.discard(jid)
            self._sem.release()

    def _run_job(self, jid: int, job: dict) -> None:
        job_driver = build_job_driver(self.base_driver, job, grant_ceiling=self.grant_ceiling)
        total = float(job.get("timeout_s") or 1800)
        factory = self.stream_driver_factory
        if factory is None:
            def factory(d, *, idle_timeout, total_timeout):
                return StreamDriver(d, idle_timeout=idle_timeout, total_timeout=total_timeout)
        sd = factory(job_driver, idle_timeout=self.idle_timeout, total_timeout=total)
        # Near-cap tightening: an unpinned job runs on the light model once
        # month spend reaches 80% of the credit. A pinned model always wins.
        model = job.get("model") or ("" if not self._tightened() else self.light_model)
        try:
            turn = sd.start(job["prompt"], None, model or None)
        except Exception as exc:
            self._finish_job(jid, job, ClaudeResult(
                text="", session_id=None, is_error=True,
                error=f"job failed to start: {exc}",
            ))
            return
        with self._state_lock:
            self._turns[jid] = turn
        self.turn_registered.set()
        # A cancel that landed between claim and registration was invisible to
        # the cancel pass (it only walks registered turns) and nothing later
        # re-reads the flag; honor it now.
        record = self.store.get(jid)
        if record and record.get("cancel_requested"):
            with self._state_lock:
                self._cancel_flagged.add(jid)
            turn.cancel()
        # The stream watchdog enforces the real ceilings; these bounds are a
        # backstop so a broken turn can never wedge the worker thread forever.
        bound = total + self.idle_timeout + 30.0
        result = turn.wait_primary(bound)
        turn.wait_finished(bound)
        if result is None:
            result = ClaudeResult(text="", session_id=None, is_error=True,
                                  error="job turn ended without a result")
        self._finish_job(jid, job, result)

    def _finish_job(self, jid: int, job: dict, result: ClaudeResult) -> None:
        with self._state_lock:
            was_cancelled = jid in self._cancel_flagged
        if not result.is_error:
            # Per the cancel contract a reply that landed before the kill is
            # preserved, so an ok primary means the job finished its work.
            status = "done"
        elif was_cancelled:
            # The flag is authoritative: the cancel pass sets it under the
            # state lock before the kill. The error text is free-form (model
            # prose, folded stderr), so matching it would misfile a genuine
            # failure that merely mentions "cancelled" and eat its ping.
            status = "cancelled"
        else:
            status = "failed"
        finished_at = time.time()
        self.store.update(jid, status=status, finished_at=finished_at, result={
            "text": result.text,
            "session_id": result.session_id,
            "is_error": result.is_error,
            "error": result.error,
            "cost_usd": result.cost_usd,
            "duration_ms": result.duration_ms,
            "context_tokens": result.context_tokens,
        })
        if self.metrics_path:
            # Outside any runner lock: telemetry file I/O must never stall the
            # cancel/claim passes or other workers.
            emit_turn(self.metrics_path, f"job:{jid}", result, None, "job", False, 1)
        if status == "cancelled":
            return  # the owner asked for the cancel; no ping either way
        if status == "failed":
            # Only a genuine failure parks: a cancel's free-form error text
            # (which may mention credit) returned above and never reaches this.
            self._maybe_park(result)
        self._deliver_result(jid, job, result, status, finished_at)

    # -- credit guard ----------------------------------------------------------

    def _tightened(self) -> bool:
        """True once month spend has reached 80% of the monthly credit."""
        if self.monthly_credit <= 0 or not self.light_model or not self.metrics_path:
            return False
        now = time.time()
        records = budget.read_metrics(self.metrics_path, budget.window(now, "month"))
        return budget.summarize(records)["total_cost"] >= 0.8 * self.monthly_credit

    def _maybe_park(self, result: ClaudeResult) -> None:
        # Credit or rate-limit pushback parks ALL claiming: every further run
        # would burn a metered call into the same wall. Pending jobs stay
        # queued; the watcher keeps running and notices expiry.
        if not self.budget_state_path or self.park_minutes <= 0:
            return
        if not is_credit_or_rate_pushback(result.error):
            return
        state = budget.BudgetState(self.budget_state_path)
        now = time.time()
        if state.park_until > now:
            return  # already parked: one ping per park
        until = now + self.park_minutes * 60.0
        state.set_park_until(until)
        hhmm = datetime.fromtimestamp(until).strftime("%H:%M")
        self._budget_ping(
            f"jobs parked until ~{hhmm}: the credit pool or rate limit pushed back")

    def _budget_ping(self, text: str) -> None:
        # Park/resume pings are templated strings by rule (clock/failure
        # driven), so they go straight to the deliver seam: no event, no gate,
        # and never a model.
        if not _notify_send(text, token=self.discord_token,
                            channel=self.notify_channel, sender=self.sender):
            log.warning("budget notify undeliverable (channel=%r): %s",
                        self.notify_channel, text)

    # -- delivery -------------------------------------------------------------

    def _deliver_result(self, jid: int, job: dict, result: ClaudeResult,
                        status: str, finished_at: float) -> None:
        ok = status == "done"
        title = job.get("title") or ""
        if ok:
            text = f'[background job #{jid} "{title}" finished]\n{result.text}'
        else:
            text = f'[background job #{jid} "{title}" failed: {result.error}]'
        # Re-read the record: turn_finished may have stamped it after the claim.
        current = self.store.get(jid) or job
        conversation_id = current.get("conversation_id") or ""
        channel_id = current.get("channel_id") or ""
        if conversation_id and channel_id and self.deliver is not None:
            try:
                if self.deliver(channel_id, conversation_id, text):
                    return  # fold-back delivered: never both paths for one job
            except Exception:
                pass
        started_at = job.get("started_at") or finished_at
        self._spine_notify(
            kind="finished" if ok else "failed",
            title=title or f"job #{jid}",
            exit_code=0 if ok else 1,
            duration_s=max(0.0, finished_at - started_at),
            tail=_tail_lines(result.text if ok else (result.error or result.text or "")),
            channel=channel_id,
        )

    def _spine_notify(self, *, kind: str, title: str, exit_code: int,
                      duration_s: float, tail: str, channel: str,
                      allow_model: bool = True) -> None:
        # Exactly watch_cmd.watch's shape: event -> gate -> compose -> deliver.
        # force=True because the owner explicitly asked for the job, so even a
        # quick success is worth the (free, templated) ping.
        event = Event(
            source="job",
            kind=kind,
            title=title,
            exit_code=exit_code,
            duration_s=duration_s,
            tail=tail,
            urgency="high" if exit_code != 0 else "normal",
        )
        if decide(event, self.watch_min_seconds, force=True) != "notify":
            return
        driver = None
        if allow_model and needs_model(event) and self.notify_driver_factory is not None:
            driver = self.notify_driver_factory()
        text = render(event, driver)
        if not _notify_send(text, token=self.discord_token,
                            channel=channel or self.notify_channel, sender=self.sender):
            # The spine is the terminal fallback; the result is stored, but a
            # ping the owner will never see must at least leave a trace.
            log.warning("job notify undeliverable (channel=%r): %s",
                        channel or self.notify_channel, text)


def _tail_lines(text: str, limit: int = 25) -> str:
    """The last ``limit`` lines, for failure triage prompts and event tails."""
    return "\n".join((text or "").splitlines()[-limit:])
