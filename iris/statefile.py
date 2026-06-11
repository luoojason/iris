"""Shared helper for the JSON state stores (jobs, usage, inbox, workspaces).

Every store recovers to a fresh, empty state when its file is corrupt so a
bad write can never take the agent down. But silently overwriting owner data
is worse than a visible gap, so the corrupt file is preserved as a .corrupt
sidecar and the loss is logged loudly. This lives on its own so the stores do
not import each other just to share it.
"""

from __future__ import annotations

import logging
from pathlib import Path

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
