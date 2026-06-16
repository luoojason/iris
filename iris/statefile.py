"""Shared helper for the JSON state stores (jobs, usage, inbox, workspaces).

Every store recovers to a fresh, empty state when its file is corrupt so a
bad write can never take the agent down. But silently overwriting owner data
is worse than a visible gap, so the corrupt file is preserved as a .corrupt
sidecar and the loss is logged loudly. This lives on its own so the stores do
not import each other just to share it.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

log = logging.getLogger("iris.statefile")


def quarantine_corrupt(path: Path, label: str) -> None:
    """Move a corrupt state file aside (once) and log it. Best-effort.

    A rename failure must not turn corruption recovery into a crash, so any
    OSError here is swallowed after logging.
    """
    log.error("%s at %s is corrupt; starting fresh (a .corrupt copy is kept)", label, path)
    sidecar = path.with_suffix(path.suffix + ".corrupt")
    try:
        if not sidecar.exists():
            path.replace(sidecar)
    except OSError:
        pass


class JsonStateFile:
    """flock + atomic-write + corruption-quarantine for one JSON state file.

    The lock-handling, atomic-replace, and corruption-recovery dance was
    copy-pasted across the stores and had already drifted (some returned [] on a
    bad file, some quarantined, one wrote its own sidecar). This is the single
    home for it; ``JsonListStore``/``JsonDictStore`` bind the default. Holders run
    a read-modify-write under ``with store.locked(): store.save(mutate(store.load()))``.
    """

    def __init__(self, path, label: str, default, *, sort_keys: bool = False):
        self.path = Path(path)
        self.label = label
        self._default = default
        self._sort_keys = sort_keys

    @contextmanager
    def locked(self):
        """Hold an exclusive cross-process lock for a read-modify-write."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is None:  # pragma: no cover - Windows
            yield
            return
        lock = self.path.with_suffix(self.path.suffix + ".lock")
        with open(lock, "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def load(self):
        """Parse the file, recovering to a fresh default on corruption.

        A bad file is quarantined (kept as a .corrupt sidecar) so owner data is
        never silently overwritten. Wrong top-level type reads as the default too.
        """
        if not self.path.exists():
            return copy.deepcopy(self._default)
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            quarantine_corrupt(self.path, self.label)
            return copy.deepcopy(self._default)
        if type(data) is not type(self._default):
            # Valid JSON of the wrong top-level shape (a hand edit) is still owner
            # data; quarantine it rather than let the next save overwrite it.
            quarantine_corrupt(self.path, self.label)
            return copy.deepcopy(self._default)
        return data

    def save(self, data) -> None:
        """Atomically replace the file (mkstemp + os.replace)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent or ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=self._sort_keys)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


class JsonListStore(JsonStateFile):
    """A JSON state file whose top level is a list."""

    def __init__(self, path, label: str, *, sort_keys: bool = False):
        super().__init__(path, label, [], sort_keys=sort_keys)


class JsonDictStore(JsonStateFile):
    """A JSON state file whose top level is a dict."""

    def __init__(self, path, label: str, *, sort_keys: bool = False):
        super().__init__(path, label, {}, sort_keys=sort_keys)
