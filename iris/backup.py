"""Whole-state snapshot and restore for the owner's JSON state stores.

Iris keeps her durable memory in a spread of small JSON files (the job and
schedule registries, reminders, goals, the usage ledger, the memory store, and
so on). A bad edit, a botched migration, or a corruption-recovery that started a
store fresh can lose owner data across several of those files at once. This
module takes a single timestamped copy of the whole set and can put it back.

Two sets matter and are kept separate on purpose:

* The **snapshot set** (:func:`STATE_FILES`) is everything worth copying, the
  tick cursors included, so a snapshot is a faithful point-in-time image.
* The **restore set** (:func:`RESTORE_FILES`) drops the tick-owned ``*.state.json``
  cursors (resume / wakes / heartbeat). Those record "how far the clock has
  read"; copying an old cursor back would let a tick replay already-fired wakes
  or re-arm spent reminders, so a restore must never touch them.

Restore carries an empty-clock guard: it refuses to overwrite a live, non-empty
jobs or schedule registry with an empty one from the snapshot, because that
would silently disarm active automation. The caller can override with
``force=True`` after being warned. A restore always auto-snapshots the current
state first, so even a forced, destructive restore is itself reversible.

All wall-clock time enters through a ``now_ts`` argument; nothing here reads the
clock on its own, so the behavior is fully deterministic under test.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .config import Config

# Config fields that name a durable owner state file. Append-only ledgers
# (metrics_file, trace_file) are logs, not state, so they are deliberately left
# out. The reminders file is not a Config field (it is resolved from
# IRIS_REMINDERS_FILE); it is added in STATE_FILES below.
_STATE_FIELDS = (
    "connections_file",
    "skill_proposals_file",
    "workspaces_file",
    "jobs_file",
    "inbox_file",
    "undelivered_file",
    "recent_turns_file",
    "resume_queue_file",
    "resume_state_file",
    "proactive_usage_cache",
    "goals_file",
    "schedules_file",
    "wakes_file",
    "wakes_state",
    "heartbeat_file",
    "heartbeat_state",
    "usage_file",
    "memory_file",
    "approvals_file",
    "session_store_path",
)

# Tick-owned cursor fields: included in a snapshot, never restored. Restoring an
# old cursor would let the clock replay or re-arm work it already did.
_CURSOR_FIELDS = ("resume_state_file", "wakes_state", "heartbeat_state")

# Registries whose accidental emptying disarms active automation. The guard
# refuses to overwrite a live non-empty one of these with an empty snapshot copy.
_CLOCK_FIELDS = ("jobs_file", "schedules_file")

MANIFEST_NAME = "manifest.json"
_BACKUPS_DIRNAME = "iris-backups"


def _reminders_file() -> str:
    """The reminders store path, resolved the same way the rest of Iris does.

    It is not a Config field: cli.py, the TUI, and the reminders MCP server all
    read IRIS_REMINDERS_FILE directly, so the snapshot honors the same env var.
    """
    return os.environ.get("IRIS_REMINDERS_FILE", "iris-reminders.json")


class EmptyClockGuard(RuntimeError):
    """Raised when a restore would replace a live, non-empty jobs/schedule
    registry with an empty one from the snapshot (and ``force`` was not set)."""


def _is_cursor(config: Config, path: str) -> bool:
    """Whether ``path`` is a tick cursor (excluded from restore).

    Matches both the configured cursor fields and the ``*.state.json`` naming
    convention, so a future cursor file is excluded even if it is not yet in
    ``_CURSOR_FIELDS``.
    """
    cursors = {getattr(config, f, "") for f in _CURSOR_FIELDS}
    if path in cursors:
        return True
    return Path(path).name.endswith(".state.json")


def _dedupe(paths: list[str]) -> list[str]:
    """Drop empties and duplicates, preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for p in paths:
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def STATE_FILES(config: Config) -> list[str]:
    """The snapshot set: every configured state-file path, cursors included.

    Empty/unset config values are skipped. The reminders file is resolved from
    IRIS_REMINDERS_FILE (it is not a Config field). Existence is not checked
    here; :func:`snapshot_state` only copies the files that actually exist.
    """
    paths = [getattr(config, field, "") or "" for field in _STATE_FIELDS]
    paths.append(_reminders_file())
    return _dedupe(paths)


def RESTORE_FILES(config: Config) -> list[str]:
    """The restore set: the snapshot set minus the tick cursors.

    A restore copies these back over the live files. The ``*.state.json`` tick
    cursors are intentionally excluded so a restore cannot replay or re-arm.
    """
    return [p for p in STATE_FILES(config) if not _is_cursor(config, p)]


def _backups_root(config: Config) -> Path:
    """The directory snapshots live under.

    Honors an optional ``backups_dir`` config field if one is ever added;
    otherwise it sits next to the jobs registry as ``<dir>/iris-backups``.
    """
    override = getattr(config, "backups_dir", "") or ""
    if override:
        return Path(override)
    return Path(config.jobs_file).parent / _BACKUPS_DIRNAME


def _stamp(now_ts: float) -> str:
    """A UTC ``YYYYmmdd-HHMMSS`` stamp for a snapshot directory name."""
    return datetime.fromtimestamp(now_ts, timezone.utc).strftime("%Y%m%d-%H%M%S")


def _registry_len(path: Path) -> int:
    """Length of a JSON-list registry file, or 0 if missing/empty/not a list.

    A file that does not exist, does not parse, or is not a list counts as
    empty, so the empty-clock guard treats any unreadable snapshot copy as a
    destructive overwrite rather than trusting it.
    """
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    return len(data) if isinstance(data, list) else 0


def snapshot_state(config: Config, *, now_ts: float, label: str = "") -> str:
    """Copy every existing state file into a new timestamped snapshot directory.

    Returns the absolute path of the snapshot directory (its basename is the
    snapshot id). A ``manifest.json`` records what was copied, which entries are
    tick cursors, and when. If two snapshots land in the same second with the
    same label, a numeric suffix keeps the directory names distinct.
    """
    root = _backups_root(config)
    base = _stamp(now_ts)
    if label:
        base = f"{base}-{label}"
    snap_dir = root / base
    n = 2
    while snap_dir.exists():
        snap_dir = root / f"{base}-{n}"
        n += 1
    snap_dir.mkdir(parents=True)

    files = []
    for src in STATE_FILES(config):
        src_path = Path(src)
        if not src_path.is_file():
            continue
        name = src_path.name
        shutil.copy2(src_path, snap_dir / name)
        files.append({
            "name": name,
            "source": str(src_path),
            "bytes": src_path.stat().st_size,
            "cursor": _is_cursor(config, src),
        })

    manifest = {
        "id": snap_dir.name,
        "created_ts": now_ts,
        "created": datetime.fromtimestamp(now_ts, timezone.utc).isoformat(),
        "label": label,
        "files": files,
    }
    (snap_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), "utf-8")
    return str(snap_dir)


def list_snapshots(config: Config) -> list[str]:
    """Absolute snapshot directories, newest first.

    Only directories that carry a ``manifest.json`` count as snapshots. Sorted
    by recorded creation time (falling back to the timestamp-prefixed name), so
    the most recent snapshot leads.
    """
    root = _backups_root(config)
    if not root.is_dir():
        return []
    snaps = []
    for child in root.iterdir():
        manifest = child / MANIFEST_NAME
        if not (child.is_dir() and manifest.is_file()):
            continue
        try:
            created = json.loads(manifest.read_text("utf-8")).get("created_ts", 0.0)
        except (OSError, json.JSONDecodeError):
            created = 0.0
        snaps.append((created, child.name, str(child)))
    snaps.sort(reverse=True)
    return [path for _, _, path in snaps]


def _resolve_snapshot(config: Config, snapshot_id: str) -> Path:
    """Resolve a snapshot id (basename) or directory path to its directory."""
    direct = Path(snapshot_id)
    if direct.is_dir():
        return direct
    candidate = _backups_root(config) / snapshot_id
    if candidate.is_dir():
        return candidate
    raise FileNotFoundError(f"no such snapshot: {snapshot_id}")


def _clock_overwrites(config: Config, snap_dir: Path) -> list[str]:
    """Names of clock registries a restore would empty (live non-empty, snapshot empty)."""
    blocked = []
    for field in _CLOCK_FIELDS:
        live = getattr(config, field, "") or ""
        if not live:
            continue
        live_path = Path(live)
        snap_copy = snap_dir / live_path.name
        if _registry_len(live_path) > 0 and _registry_len(snap_copy) == 0:
            blocked.append(live_path.name)
    return blocked


def restore_state(config: Config, snapshot_id: str, *, now_ts: float,
                  force: bool = False) -> str:
    """Copy a snapshot's files back over the live state, then return the snapshot id.

    The current state is auto-snapshotted first (label ``pre-restore``) so the
    restore is reversible. The empty-clock guard refuses to overwrite a live,
    non-empty jobs or schedule registry with an empty snapshot copy unless
    ``force`` is set. Tick cursors are never restored (see :func:`RESTORE_FILES`).
    """
    snap_dir = _resolve_snapshot(config, snapshot_id)

    blocked = _clock_overwrites(config, snap_dir)
    if blocked and not force:
        names = ", ".join(blocked)
        raise EmptyClockGuard(
            f"refusing to restore: snapshot {snap_dir.name} would replace a live, "
            f"non-empty registry ({names}) with an empty one, disarming active "
            f"automation. Pass force=True to override (the current state is "
            f"auto-snapshotted first, so this is reversible).")

    # Always preserve the current state before overwriting it, even on a forced
    # destructive restore, so nothing is lost without a recovery point.
    snapshot_state(config, now_ts=now_ts, label="pre-restore")

    for dest in RESTORE_FILES(config):
        dest_path = Path(dest)
        src = snap_dir / dest_path.name
        if not src.is_file():
            continue
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest_path)
    return snap_dir.name
