"""Tests for the whole-state snapshot/restore helpers (iris/backup.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from iris.backup import (
    EmptyClockGuard,
    RESTORE_FILES,
    STATE_FILES,
    list_snapshots,
    restore_state,
    snapshot_state,
)
from iris.config import Config

# A fixed clock so snapshot directory names and manifests are deterministic.
NOW = 1_700_000_000.0  # 2023-11-14 22:13:20 UTC

# Every Config field that names a state file, so the test config keeps all of
# them inside tmp_path and nothing ever resolves to a real cwd-relative file.
_FILE_FIELDS = (
    "connections_file", "skill_proposals_file", "workspaces_file", "jobs_file",
    "inbox_file", "undelivered_file", "recent_turns_file", "resume_queue_file",
    "resume_state_file", "proactive_usage_cache", "goals_file", "schedules_file",
    "wakes_file", "wakes_state", "heartbeat_file", "heartbeat_state",
    "usage_file", "memory_file", "approvals_file", "session_store_path",
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Keep every relative state path and the reminders file inside tmp_path."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IRIS_REMINDERS_FILE", str(tmp_path / "iris-reminders.json"))


def _config(tmp_path, **overrides):
    """A Config whose every state file lives under ``tmp_path``."""
    defaults = {f: getattr(Config(), f) for f in _FILE_FIELDS}
    defaults.update(overrides)
    abs_fields = {k: ("" if v == "" else str(tmp_path / Path(v).name))
                  for k, v in defaults.items()}
    return Config(**abs_fields)


def _write_json(path, data):
    Path(path).write_text(json.dumps(data), "utf-8")


def test_state_files_skips_empty_and_separates_restore_set(tmp_path):
    config = _config(tmp_path, memory_file="")  # an unset field is skipped

    snap_set = STATE_FILES(config)
    restore_set = RESTORE_FILES(config)

    assert config.jobs_file in snap_set
    assert "" not in snap_set  # the unset memory_file is skipped
    assert str(tmp_path / "iris-reminders.json") in snap_set  # reminders from env

    # Cursors are in the snapshot set but excluded from the restore set.
    for cursor in (config.wakes_state, config.resume_state_file, config.heartbeat_state):
        assert cursor in snap_set
        assert cursor not in restore_set


def test_snapshot_creates_dir_manifest_and_copies(tmp_path):
    config = _config(tmp_path)
    _write_json(config.jobs_file, [{"id": 1, "state": "running"}])
    _write_json(config.memory_file, {"facts": ["x"]})
    _write_json(config.wakes_state, {"cursor": 42})  # a cursor: snapshotted, not restored

    snap = Path(snapshot_state(config, now_ts=NOW))

    assert snap.is_dir()
    assert (snap / "iris-jobs.json").is_file()
    assert (snap / "iris-memory.json").is_file()
    assert (snap / "iris-wakes.state.json").is_file()  # cursor is in the snapshot

    manifest = json.loads((snap / "manifest.json").read_text("utf-8"))
    assert manifest["created_ts"] == NOW
    names = {f["name"]: f for f in manifest["files"]}
    assert "iris-jobs.json" in names
    assert names["iris-wakes.state.json"]["cursor"] is True
    assert names["iris-jobs.json"]["cursor"] is False
    # The copy is faithful.
    assert json.loads((snap / "iris-jobs.json").read_text("utf-8")) == [
        {"id": 1, "state": "running"}]


def test_list_snapshots_newest_first(tmp_path):
    config = _config(tmp_path)
    _write_json(config.jobs_file, [{"id": 1}])
    first = snapshot_state(config, now_ts=NOW)
    second = snapshot_state(config, now_ts=NOW + 3600)

    snaps = list_snapshots(config)
    assert snaps[0] == second  # newest leads
    assert snaps[1] == first
    assert len(snaps) == 2


def test_restore_brings_files_back(tmp_path):
    config = _config(tmp_path)
    _write_json(config.jobs_file, [{"id": 1}, {"id": 2}])
    _write_json(config.memory_file, {"facts": ["original"]})

    snap = snapshot_state(config, now_ts=NOW)

    # Mutate the live state after the snapshot.
    _write_json(config.jobs_file, [{"id": 99}])
    _write_json(config.memory_file, {"facts": ["clobbered"]})

    restore_state(config, snap, now_ts=NOW + 10)

    assert json.loads(Path(config.jobs_file).read_text("utf-8")) == [{"id": 1}, {"id": 2}]
    assert json.loads(Path(config.memory_file).read_text("utf-8")) == {"facts": ["original"]}


def test_restore_does_not_replay_tick_cursor(tmp_path):
    config = _config(tmp_path)
    _write_json(config.jobs_file, [{"id": 1}])
    _write_json(config.wakes_state, {"cursor": "old"})

    snap = snapshot_state(config, now_ts=NOW)

    # The tick advances its cursor after the snapshot.
    _write_json(config.wakes_state, {"cursor": "new"})

    restore_state(config, snap, now_ts=NOW + 10)

    # The cursor is left at its current value, never rolled back to the snapshot.
    assert json.loads(Path(config.wakes_state).read_text("utf-8")) == {"cursor": "new"}


def test_empty_clock_guard_refuses_destructive_restore(tmp_path):
    config = _config(tmp_path)
    # Snapshot taken while the jobs registry was empty.
    _write_json(config.jobs_file, [])
    snap = snapshot_state(config, now_ts=NOW)

    # Now there is active automation registered.
    _write_json(config.jobs_file, [{"id": 1, "state": "running"}])

    with pytest.raises(EmptyClockGuard):
        restore_state(config, snap, now_ts=NOW + 10)

    # The live registry is untouched by the refused restore.
    assert json.loads(Path(config.jobs_file).read_text("utf-8")) == [
        {"id": 1, "state": "running"}]


def test_empty_clock_guard_can_be_forced_with_autosnapshot(tmp_path):
    config = _config(tmp_path)
    _write_json(config.jobs_file, [])
    snap = snapshot_state(config, now_ts=NOW)
    _write_json(config.jobs_file, [{"id": 1}])

    before = list_snapshots(config)
    restore_state(config, snap, now_ts=NOW + 10, force=True)

    # Forced: the empty registry is restored over the live one...
    assert json.loads(Path(config.jobs_file).read_text("utf-8")) == []
    # ...but the pre-restore auto-snapshot preserved the disarmed state.
    after = list_snapshots(config)
    assert len(after) == len(before) + 1


def test_restore_auto_snapshots_current_state(tmp_path):
    config = _config(tmp_path)
    _write_json(config.jobs_file, [{"id": 1}])
    snap = snapshot_state(config, now_ts=NOW)
    _write_json(config.jobs_file, [{"id": 1}, {"id": 2}])

    before = len(list_snapshots(config))
    restore_state(config, snap, now_ts=NOW + 10)
    after = len(list_snapshots(config))

    assert after == before + 1  # a pre-restore snapshot was added
