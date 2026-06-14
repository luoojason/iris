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
import re
import signal
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
from .statefile import quarantine_corrupt
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
    # Unlocks no built-ins: the capability arrives as the Playwright MCP
    # server, wired into the job's mcp config by build_job_driver.
    "browser": (),
}

# How much of a report folds back into the next chat turn (context economy).
# The Discord ping carries the FULL report, split across messages.
REPORT_FOLD_CAP = 1500
DISCORD_MESSAGE_LIMIT = 2000

_ACTIVE_STATES = ("pending", "running")
_TERMINAL_STATES = ("done", "failed", "cancelled")


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

    def __init__(self, path: str | os.PathLike[str], keep: Optional[int] = None):
        self.path = Path(path)
        # When set, add() auto-prunes terminal jobs past this many. None means
        # no auto-prune (the default; tests and ad-hoc readers opt out).
        self.keep = keep

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
            quarantine_corrupt(self.path, "job registry")
            return []

    def _save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(items, handle, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def add(self, title: str, instructions: str, grants: list[str],
            workspace: str, channel_id: str, state: str = "pending",
            admit_below: Optional[int] = None, heavy: bool = False) -> dict:
        """Record a job. With ``admit_below``, the active-jobs admission check
        happens under the same lock as the insert (no TOCTOU between counting
        and adding); the returned dict carries an ephemeral ``admitted`` flag
        that is never persisted."""
        with self._locked():
            items = self._load()
            active = sum(1 for j in items if j.get("state") in _ACTIVE_STATES)
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
                "claude_pid": None,
                "report": "",
                "error": None,
                "artifacts": [],
                "report_delivered": False,
                "channel_id": channel_id,
                "heavy": heavy,
            }
            items.append(job)
            returned = dict(job)
            returned["admitted"] = admit_below is None or active < admit_below
            if self.keep is not None:
                items, _ = _apply_prune(items, self.keep)
            self._save(items)
            return returned

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
                    result = dict(job)
                    # Auto-prune when a job lands in a terminal state: that is
                    # when registry growth happens, and the just-transitioned
                    # job (highest id) always survives the cull.
                    if self.keep is not None and to_state in _TERMINAL_STATES:
                        items, _ = _apply_prune(items, self.keep)
                    self._save(items)
                    return result
            return None

    def prune(self, keep: int) -> int:
        """Drop terminal jobs past the most-recent ``keep``. Returns count dropped."""
        with self._locked():
            items = self._load()
            remaining, dropped = _apply_prune(items, keep)
            if dropped:
                self._save(remaining)
            return dropped

    def count_active(self) -> int:
        return sum(1 for j in self._load() if j.get("state") in _ACTIVE_STATES)


def kill_process_group(pid) -> bool:
    """SIGKILL a pid's whole process group. False if the pid is gone/invalid."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def cancel(store: JobStore, job_id: int, *, kill=kill_process_group) -> str:
    """Cancel a job, killing its runner and its claude turn. Refuses on a lost race.

    Shared by the jobs MCP tool and the console so the transition-first,
    kill-both-process-groups, refuse-on-race logic lives in exactly one place.
    """
    job = store.get(job_id)
    if job is None:
        return f"No job #{job_id}."
    # Transition-first: the store's guard decides who wins a race with the
    # runner, so a cancel is never claimed unless it actually stuck.
    if store.transition(job_id, ("pending", "parked"), "cancelled", finished_ts=time.time()):
        return f"Cancelled job #{job_id} before it started."
    job = store.get(job_id)
    if job and job["state"] == "running":
        # The claude child runs in its own session; kill BOTH groups or the
        # turn keeps burning credit and running tools after the cancel.
        killed_runner = kill(job.get("pid"))
        # Re-read: claude_pid may have been recorded during the kill window
        # (the runner records it only once the claude child spawns).
        fresh = store.get(job_id) or job
        killed_claude = bool(fresh.get("claude_pid")) and kill(fresh.get("claude_pid"))
        if store.transition(job_id, ("running",), "cancelled", finished_ts=time.time()):
            suffix = "" if (killed_runner or killed_claude) else " (its runner was already gone)"
            return f"Cancelled job #{job_id}.{suffix}"
        job = store.get(job_id)
    return f"Job #{job_id} is already {job['state'] if job else 'gone'}."


def _apply_prune(items: list[dict], keep: int) -> tuple[list[dict], int]:
    """Return (items, dropped) with terminal jobs beyond the newest ``keep`` removed.

    Active jobs (pending/running/parked) are always kept. Recency is by id,
    which is monotonic, so the highest ids survive.
    """
    if keep < 0:
        return items, 0
    terminal = [j for j in items if j.get("state") in _TERMINAL_STATES]
    if len(terminal) <= keep:
        return items, 0
    oldest = sorted(terminal, key=lambda j: j.get("id", 0))[: len(terminal) - keep]
    drop_ids = {j.get("id") for j in oldest}
    # Never drop the highest id overall: add() derives the next id from
    # max(existing)+1, so the top id must survive to anchor monotonicity.
    # Without this, prune --keep 0 on an all-terminal store would empty the
    # file and the next add() would reuse id #1.
    anchor = max((j.get("id", 0) for j in items), default=0)
    drop_ids.discard(anchor)
    remaining = [
        j for j in items
        if not (j.get("state") in _TERMINAL_STATES and j.get("id") in drop_ids)
    ]
    return remaining, len(drop_ids)


def repair_dead_runners(store: JobStore) -> int:
    """Flip jobs whose runner pid is gone to ``failed``.

    Covers two windows: ``running`` jobs whose runner died mid-flight, and
    ``pending`` jobs that were spawned (a pid was recorded) but whose runner
    died before it could even take the pending->running transition — without
    this they would consume a jobs_max slot forever. Pending jobs with no pid
    are genuinely queued and stay untouched. There is no poller; this runs on
    owner-driven touches (list, status, start) so a crashed runner is
    discovered the next time anyone looks.
    """
    repaired = 0
    for job in store.all():
        state = job.get("state")
        if state not in ("running", "pending"):
            continue
        pid = job.get("pid")
        if state == "pending" and pid is None:
            continue  # queued, never spawned
        if isinstance(pid, int) and pid > 0 and _pid_alive(pid):
            continue
        error = ("the job runner died" if state == "running"
                 else "the job runner died before starting")
        if store.transition(job["id"], (state,), "failed",
                            error=error, finished_ts=time.time()):
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


def write_browser_mcp_config(config: Config) -> str:
    """Write the job-scoped mcp config that carries the Playwright server.

    The browser gets its own persistent profile directory (cookies and logins
    the owner deliberately gave the agent), never the owner's real browser
    profile. The command is owner-configured (IRIS_BROWSER_MCP_CMD); the
    profile flag is appended here so a rewired command cannot silently drop
    the isolation.
    """
    import shlex

    argv = shlex.split(config.browser_mcp_cmd)
    if not argv:
        raise ValueError("IRIS_BROWSER_MCP_CMD is empty")
    profile = str(Path(config.browser_profile_dir).resolve())
    spec = {
        "mcpServers": {
            "playwright": {
                "command": argv[0],
                "args": argv[1:] + ["--user-data-dir", profile],
            }
        }
    }
    fd, path = tempfile.mkstemp(prefix="iris-job-mcp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(spec, handle, indent=2)
    return path


def build_job_driver(config: Config, job: dict, workspace_path: Optional[str],
                     child_pid_callback=None) -> ClaudeDriver:
    """The job's ClaudeDriver: same hardened path as chat, wider grants.

    Two deliberate deviations from the chat defaults:

    * ``restrict_builtin_tools=False``: the denylist here is the *derived*
      one. With every grant given it derives to the empty tuple, and the
      driver treats a falsy explicit denylist as unset and would silently
      fall back to the FULL default — re-denying every granted tool.
    * ``cwd``: the agent's own directory holds .env and the state files, and
      the Read tool is always available to a job. The child runs in the
      workspace instead, or a throwaway scratch dir when there is none.
    """
    grants = list(job.get("grants") or ["subagents"])
    cwd = workspace_path or tempfile.mkdtemp(prefix="iris-job-")
    allowed = job_allowed_builtins(grants)
    disallowed = job_disallowed(grants)
    mcp_config = None
    if "browser" in grants:
        mcp_config = write_browser_mcp_config(config)
        # The bare server name pre-approves every tool the server exposes;
        # --strict-mcp-config (set by the driver whenever mcp_config is given)
        # keeps the job from seeing any other server on the host. The
        # owner-configured deny list (default: in-page code execution only) is
        # then applied by name on top, since deny outranks allow.
        allowed = allowed + ["mcp__playwright"]
        disallowed = disallowed + tuple(
            f"mcp__playwright__{tool}" for tool in config.browser_deny_tools
        )
    # Heavy jobs escalate to the stronger model; everyday jobs run on the base.
    model = config.job_model_heavy if job.get("heavy") else (config.job_model or config.model)
    return ClaudeDriver(
        claude_bin=config.claude_bin,
        model=model,
        append_system_prompt_file=config.job_persona or None,
        mcp_config=mcp_config,
        permission_mode=config.permission_mode,
        allowed_tools=allowed or None,
        disallowed_tools=disallowed,
        restrict_builtin_tools=False,
        disable_auto_memory=config.disable_auto_memory,
        add_dirs=[workspace_path] if workspace_path else None,
        cwd=cwd,
        child_pid_callback=child_pid_callback,
        timeout=config.job_timeout,
        max_retries=config.max_retries,
        retry_base_delay=config.retry_base_delay,
        timeout_max_retries=0,
    )


def spawn_runner(job_id: int, *, store: Optional[JobStore] = None, popen=None) -> int:
    """Launch the detached runner for a recorded job, recording its pid.

    The pid lands in the job row immediately (the runner re-records the same
    value on its pending->running transition), so a runner that dies before
    that transition is still discoverable by repair_dead_runners instead of
    consuming a jobs_max slot forever.
    """
    popen = popen or subprocess.Popen
    proc = popen(
        [sys.executable, "-m", "iris", "job-run", str(job_id)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    pid = getattr(proc, "pid", None)
    if store is not None and isinstance(pid, int):
        store.update(job_id, pid=pid)
    return pid if isinstance(pid, int) else 0


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
    guard=None,
    verify=None,
) -> int:
    """Run one recorded job to completion. This IS the detached runner.

    Exactly one model call happens here (the job turn). Completion is
    delivered without the model: a REST ping plus a fold-back inbox note.
    """
    store = store or JobStore(config.jobs_file, keep=config.jobs_keep)
    workspace_store = workspace_store or WorkspaceStore(config.workspaces_file)
    inbox = inbox or Inbox(config.inbox_file)
    driver_factory = driver_factory or build_job_driver
    if guard is None:
        from .usage import CreditGuard
        guard = CreditGuard.from_config(config)
    # Independent verification of the result, when enabled and the credit guard
    # isn't parked (the job turn already ran; a parked guard skips the extra,
    # cheap reviewer call rather than spending on it).
    if verify is None and getattr(config, "job_verify_enabled", False) and not guard.should_park():
        from .verify import verify_result
        verify = lambda instructions, report: verify_result(config, instructions, report)
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

    def deliver(text: str, problems: list = ()) -> bool:
        # Discord gets the FULL report, split across messages so nothing the job
        # said is ever cut. The fold-back inbox note stays capped so a long
        # report cannot blow the next chat turn's context budget.
        full = (text or "").strip()
        for problem in problems:
            full += "\n" + str(problem)
        delivered = True
        if channel and token:
            from .textutil import chunk_text
            for piece in chunk_text(full, DISCORD_MESSAGE_LIMIT):
                if not send_message(channel, piece, token):
                    delivered = False
                    log.warning("could not ping channel %s for job %s", channel, job_id)
        note = _head(text)
        for problem in problems:
            note += "\n" + str(problem)
        # Tag the note with the channel this job reports to, so it folds into
        # that conversation only and never bleeds into an unrelated thread.
        inbox.append(note, conversation_id=(f"discord:{channel}" if channel else None))
        return delivered

    workspace_path: Optional[str] = None
    if job.get("workspace"):
        workspace_path = workspace_store.resolve(job["workspace"])
        if workspace_path is None:
            error = f"unknown workspace {job['workspace']!r}"
            store.transition(job_id, ("running",), "failed",
                             error=error, finished_ts=time.time())
            deliver(f"job #{job_id} ({job['title']}) failed: {error}")
            return 1

    def record_child(pid: int) -> None:
        # So a cancel can kill the claude tree: it runs in its OWN session
        # (driver hardening), so killing the runner alone would orphan it.
        store.update(job_id, claude_pid=pid)

    try:
        driver = driver_factory(config, job, workspace_path, record_child)
        result = driver.run(job["instructions"])
    except Exception as exc:
        # ClaudeError (binary missing) or anything else: the job must never
        # be left 'running' with no ping — the owner is never left guessing.
        log.exception("job %s crashed while launching claude", job_id)
        store.transition(job_id, ("running",), "failed",
                         error=f"the job turn crashed: {exc}", finished_ts=time.time())
        deliver(f"job #{job_id} ({job['title']}) failed: the job turn crashed: {exc}")
        return 1
    guard.record("job", result)

    if result.is_error:
        error = result.error or "the job turn failed"
        store.transition(job_id, ("running",), "failed",
                         error=error, finished_ts=time.time())
        deliver(f"job #{job_id} ({job['title']}) failed: {error}")
        return 1

    report = result.text or ""
    try:
        files, problems = collect_artifacts(report, workspace_path)
        artifact_names = [str(Path(f).relative_to(Path(workspace_path).resolve())) if workspace_path else f
                          for f in files]
    except Exception as exc:
        # The turn is already paid for; its report must survive the crash.
        log.exception("job %s finished but artifact handling crashed", job_id)
        store.transition(job_id, ("running",), "failed",
                         error=f"finished, but artifact handling crashed: {exc}",
                         report=report, finished_ts=time.time())
        deliver(f"job #{job_id} ({job['title']}) finished, but artifact handling crashed: {exc}")
        return 1

    # Independent check that the report actually satisfies the ask. It only
    # annotates (the result is always delivered) and never raises: a crashing or
    # unreadable reviewer fails open to "couldn't verify" rather than blocking.
    verified: Optional[bool] = None
    verify_reason = ""
    if verify is not None:
        try:
            verdict = verify(job["instructions"], report)
        except Exception:
            log.exception("job %s verification crashed; delivering unverified", job_id)
            verdict = {"ok": None, "reason": "verification crashed"}
        verified = verdict.get("ok")
        verify_reason = verdict.get("reason", "") or ""

    final = store.transition(job_id, ("running",), "done",
                             report=report, artifacts=artifact_names,
                             verified=verified, finished_ts=time.time())
    if final is None:
        # The job left 'running' under us (an owner cancel won the race).
        # Don't follow a cancel with a confusing 'finished' ping.
        log.info("job %s was cancelled mid-run; skipping delivery", job_id)
        return 0

    banner = ""
    if verified is False:
        banner = (f"[verification flag] an independent check thinks this may not fully "
                  f"satisfy the task: {verify_reason}\n")
    if deliver(f"{banner}job #{job_id} ({job['title']}) finished: {report}", problems):
        store.update(job_id, report_delivered=True)

    try:
        for path in files:
            if channel and token:
                res = send_file(channel, path, f"job #{job_id} artifact", token)
                if isinstance(res, dict) and res.get("error"):
                    inbox.append(f"job #{job_id}: artifact {Path(path).name} failed to upload: {res['error']}",
                                 conversation_id=(f"discord:{channel}" if channel else None))
    except Exception:
        log.exception("job %s artifact upload crashed after completion", job_id)
    return 0


def _header_safe(filename: str) -> str:
    """A filename safe to embed in a multipart header.

    A job with file grants can create files whose names carry quotes or
    control bytes; interpolated raw into Content-Disposition those would
    terminate the header and inject parts into the authenticated request.
    The real name still reaches Discord via the JSON payload (json.dumps).
    """
    cleaned = re.sub(r'[^\x20-\x7e]', "_", filename)
    return cleaned.replace('"', "_").replace("\\", "_") or "artifact"


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
            f'filename="{_header_safe(filename)}"\r\nContent-Type: application/octet-stream\r\n\r\n'
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
