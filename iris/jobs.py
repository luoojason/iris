"""Background job coordination: the job registry and per-job driver policy.

JobStore is a file-backed registry shaped like ReminderStore (fcntl sidecar
lock, atomic tempfile+os.replace writes, corrupt-tolerant load) so the MCP
jobs server subprocess and the bot process can share it safely, and every
state change hits disk before returning. build_job_driver derives a per-job
ClaudeDriver from the chat driver without ever mutating it.

Faking seams: stores take a path (tests use tmp_path); the drivers built here
are pure dataclasses whose build_command output is asserted directly.
"""

from __future__ import annotations

import dataclasses
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Sequence

from .driver import DANGEROUS_BUILTINS, ClaudeDriver

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

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
    """
    granted = set(job.get("grants") or ()) & set(grant_ceiling)
    return dataclasses.replace(
        base_driver,
        timeout=float(job.get("timeout_s") or base_driver.timeout),
        model=job.get("model") or base_driver.model,
        append_system_prompt=JOB_PREAMBLE,
        disallowed_tools=tuple(t for t in DANGEROUS_BUILTINS if t not in granted),
    )
