"""Background jobs: the store, the grant model, and the detached runner.

A job is one ``claude -p`` turn that runs detached from chat, with its own
(wider) tool grants and a long timeout. Chat stays locked down; depth lives
here: the job denylist re-allows Task/Agent so a job can fan out into
subagents internally. Everything is behind ``IRIS_JOBS``.
See docs/superpowers/specs/2026-06-08-job-coordinator-design.md.

Three invariants this module carries:

* **Derived denylists.** An explicit ``disallowed_tools`` REPLACES the
  driver's default denylist, so the job denylist is *derived* from
  ``DANGEROUS_BUILTINS`` by subtracting granted tools — never hand-written.
* **Zero idle inference.** The runner makes exactly one model call: the job
  turn itself, which traces back to an explicit owner request. Completion is
  delivered by plain Discord REST and the fold-back inbox; nothing polls.
* **Names, not paths.** A job references its workspace by registered name;
  resolution happens here, behind the model boundary.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .config import Config
from .driver import DANGEROUS_BUILTINS, ClaudeDriver
from .inbox import Inbox
from .workspaces import WorkspaceStore, collect_artifacts

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

log = logging.getLogger("iris.jobs")

# Grant names the owner (ceiling) and the model (per-job request) speak in,
# mapped to the dangerous built-ins they unlock. ``subagents`` is always
# granted to a job; that is the point of a job.
GRANT_TOOLS = {
    "subagents": ("Task", "Agent"),
    "shell": ("Bash", "BashOutput", "KillShell"),
    "files": ("Write", "Edit", "NotebookEdit"),
}

# How much of a report travels in the fold-back note and the Discord ping.
REPORT_FOLD_CAP = 1500

_ACTIVE_STATES = ("pending", "running")


def parse_grants(spec: str) -> list[str]:
    """Parse a comma list of grant names, validating against GRANT_TOOLS."""
    names: list[str] = []
    for raw in (spec or "").split(","):
        name = raw.strip().lower()
        if not name:
            continue
        if name not in GRANT_TOOLS:
            known = ", ".join(sorted(GRANT_TOOLS))
            raise ValueError(f"unknown grant {name!r}; known grants: {known}")
        if name not in names:
            names.append(name)
    return names


def clamp_grants(requested: list[str], ceiling: list[str]) -> tuple[list[str], list[str]]:
    """Clamp a job's requested grants to the owner's IRIS_JOB_GRANTS ceiling.

    Returns ``(granted, clamped)``. ``subagents`` is always granted; every
    other grant must appear in the ceiling or it lands in ``clamped`` so the
    model can tell the owner what was refused.
    """
    granted = ["subagents"]
    clamped: list[str] = []
    for name in requested:
        if name == "subagents":
            continue
        if name in ceiling:
            granted.append(name)
        else:
            clamped.append(name)
    return granted, clamped


def _unlocked(grants: list[str]) -> set:
    tools: set = set()
    for name in grants:
        tools.update(GRANT_TOOLS.get(name, ()))
    return tools


def job_disallowed(grants: list[str]) -> tuple:
    """The job denylist: DANGEROUS_BUILTINS minus what the grants unlock.

    Derived, never hand-written: an explicit disallowed_tools REPLACES the
    driver's default, so this must keep tracking DANGEROUS_BUILTINS as it
    grows.
    """
    unlocked = _unlocked(grants)
    return tuple(tool for tool in DANGEROUS_BUILTINS if tool not in unlocked)


def job_allowed_builtins(grants: list[str]) -> list[str]:
    """Granted built-ins, pre-approved so permission mode default runs them."""
    allowed: list[str] = []
    for name in grants:
        for tool in GRANT_TOOLS.get(name, ()):
            if tool not in allowed:
                allowed.append(tool)
    return allowed


class JobStore:
    """The job registry: a JSON list with a cross-process lock, like reminders."""

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

    def add(self, title: str, instructions: str, grants: list[str],
            workspace: str, channel_id: str, state: str = "pending") -> dict:
        with self._locked():
            items = self._load()
            job = {
                "id": max((int(i.get("id", 0)) for i in items), default=0) + 1,
                "title": title,
                "instructions": instructions,
                "grants": list(grants),
                "workspace": workspace,
                "state": state,
                "created_ts": time.time(),
                "started_ts": None,
                "finished_ts": None,
                "pid": None,
                "report": "",
                "error": None,
                "artifacts": [],
                "report_delivered": False,
                "channel_id": channel_id,
            }
            items.append(job)
            self._save(items)
            return dict(job)

    def get(self, job_id: int) -> Optional[dict]:
        for job in self._load():
            if job.get("id") == job_id:
                return job
        return None

    def all(self) -> list[dict]:
        return sorted(self._load(), key=lambda j: j.get("id", 0))

    def update(self, job_id: int, **fields) -> Optional[dict]:
        with self._locked():
            items = self._load()
            for job in items:
                if job.get("id") == job_id:
                    job.update(fields)
                    self._save(items)
                    return dict(job)
            return None

    def transition(self, job_id: int, from_states: tuple, to_state: str, **fields) -> Optional[dict]:
        """Atomically move a job between states; None if it was not in from_states.

        This is the race guard: two runners (or a cancel racing a start) both
        try the transition under the lock and exactly one wins.
        """
        with self._locked():
            items = self._load()
            for job in items:
                if job.get("id") == job_id:
                    if job.get("state") not in from_states:
                        return None
                    job["state"] = to_state
                    job.update(fields)
                    self._save(items)
                    return dict(job)
            return None

    def count_active(self) -> int:
        return sum(1 for j in self._load() if j.get("state") in _ACTIVE_STATES)


def repair_dead_runners(store: JobStore) -> int:
    """Flip ``running`` jobs whose runner pid is gone to ``failed``.

    There is no poller; this runs on owner-driven touches (list, status,
    start) so a crashed runner is discovered the next time anyone looks.
    """
    repaired = 0
    for job in store.all():
        if job.get("state") != "running":
            continue
        pid = job.get("pid")
        if isinstance(pid, int) and pid > 0 and _pid_alive(pid):
            continue
        if store.transition(job["id"], ("running",), "failed",
                            error="the job runner died", finished_ts=time.time()):
            repaired += 1
    return repaired


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - someone else's pid
        return True
    return True


def build_job_driver(config: Config, job: dict, workspace_path: Optional[str]) -> ClaudeDriver:
    """The job's ClaudeDriver: same hardened path as chat, wider grants."""
    grants = list(job.get("grants") or ["subagents"])
    return ClaudeDriver(
        claude_bin=config.claude_bin,
        model=config.job_model or config.model,
        append_system_prompt_file=config.job_persona or None,
        permission_mode=config.permission_mode,
        allowed_tools=job_allowed_builtins(grants) or None,
        disallowed_tools=job_disallowed(grants),
        disable_auto_memory=config.disable_auto_memory,
        add_dirs=[workspace_path] if workspace_path else None,
        timeout=config.job_timeout,
        max_retries=config.max_retries,
        retry_base_delay=config.retry_base_delay,
        timeout_max_retries=0,
    )


def spawn_runner(job_id: int, *, popen=None) -> None:
    """Launch the detached runner for a recorded job."""
    popen = popen or subprocess.Popen
    popen(
        [sys.executable, "-m", "iris", "job-run", str(job_id)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _head(text: str, cap: int = REPORT_FOLD_CAP) -> str:
    text = (text or "").strip()
    if len(text) <= cap:
        return text
    return text[:cap] + " …[truncated]"


def run_job(
    job_id: int,
    config: Config,
    *,
    store: Optional[JobStore] = None,
    workspace_store: Optional[WorkspaceStore] = None,
    inbox: Optional[Inbox] = None,
    driver_factory=None,
    send_message=None,
    send_file=None,
) -> int:
    """Run one recorded job to completion. This IS the detached runner.

    Exactly one model call happens here (the job turn). Completion is
    delivered without the model: a REST ping plus a fold-back inbox note.
    """
    store = store or JobStore(config.jobs_file)
    workspace_store = workspace_store or WorkspaceStore(config.workspaces_file)
    inbox = inbox or Inbox(config.inbox_file)
    driver_factory = driver_factory or build_job_driver
    if send_message is None:
        from .reminders import send_discord_message as send_message
    send_file = send_file or send_discord_file

    job = store.transition(job_id, ("pending",), "running",
                           started_ts=time.time(), pid=os.getpid())
    if job is None:
        log.warning("job %s is not pending; refusing to run it", job_id)
        return 1

    channel = job.get("channel_id") or config.home_channel
    token = config.discord_token

    def deliver(text: str) -> None:
        note = _head(text)
        if channel and token:
            if not send_message(channel, note, token):
                log.warning("could not ping channel %s for job %s", channel, job_id)
        inbox.append(note)

    workspace_path: Optional[str] = None
    if job.get("workspace"):
        workspace_path = workspace_store.resolve(job["workspace"])
        if workspace_path is None:
            error = f"unknown workspace {job['workspace']!r}"
            store.transition(job_id, ("running",), "failed",
                             error=error, finished_ts=time.time())
            deliver(f"job #{job_id} ({job['title']}) failed: {error}")
            return 1

    driver = driver_factory(config, job, workspace_path)
    result = driver.run(job["instructions"])

    if result.is_error:
        error = result.error or "the job turn failed"
        store.transition(job_id, ("running",), "failed",
                         error=error, finished_ts=time.time())
        deliver(f"job #{job_id} ({job['title']}) failed: {error}")
        return 1

    report = result.text or ""
    files, problems = collect_artifacts(report, workspace_path)
    artifact_names = [str(Path(f).relative_to(Path(workspace_path).resolve())) if workspace_path else f
                      for f in files]
    store.transition(job_id, ("running",), "done",
                     report=report, artifacts=artifact_names,
                     finished_ts=time.time())

    summary = f"job #{job_id} ({job['title']}) finished: {report}"
    if problems:
        summary += "\n" + "\n".join(problems)
    deliver(summary)

    for path in files:
        if channel and token:
            res = send_file(channel, path, f"job #{job_id} artifact", token)
            if isinstance(res, dict) and res.get("error"):
                inbox.append(f"job #{job_id}: artifact {Path(path).name} failed to upload: {res['error']}")
    return 0


def send_discord_file(channel_id: str, file_path: str, content: str, token: str) -> dict:
    """Upload one file as a Discord message. Returns {ok} or {error}."""
    filename = Path(file_path).name
    try:
        with open(file_path, "rb") as handle:
            blob = handle.read()
    except OSError as exc:
        return {"error": str(exc)}
    boundary = f"----iris{uuid.uuid4().hex}"
    payload = json.dumps({"content": content[:2000],
                          "attachments": [{"id": 0, "filename": filename}]})
    body = b"".join([
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
            f"Content-Type: application/json\r\n\r\n{payload}\r\n"
        ).encode(),
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="files[0]"; '
            f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode() + blob + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        f"https://discord.com/api/v10/channels/{channel_id}/messages",
        data=body, method="POST",
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "iris (https://github.com/luoojason/iris, 0.1)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return {"ok": True, "status": resp.status}
    except urllib.error.HTTPError as exc:
        return {"error": f"HTTP {exc.code}"}
    except OSError as exc:
        return {"error": str(exc)}
