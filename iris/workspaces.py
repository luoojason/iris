"""Owner-registered repo workspaces, and the ARTIFACT: hand-back rules.

The model never names filesystem paths. The owner registers directories under
short names with ``iris workspaces add <name> <path>``; jobs refer to those
names only, and the job runner resolves them. See
docs/superpowers/specs/2026-06-09-repo-workspaces-design.md.

The ARTIFACT: convention is how a job hands files to the owner: lines of the
form ``ARTIFACT: relative/path`` in its report name files inside its
workspace. Collection is containment-checked (a symlink cannot smuggle a file
out) and capped at ARTIFACT_MAX_FILES / ARTIFACT_MAX_BYTES, and every skipped
or rejected artifact is reported by name, never silently dropped.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

ARTIFACT_MAX_FILES = 5
ARTIFACT_MAX_BYTES = 8 * 1024 * 1024

_ARTIFACT_LINE = re.compile(r"^ARTIFACT:\s*(.+?)\s*$", re.MULTILINE)


def valid_name(name: str) -> bool:
    """Whether a workspace name is well-formed (short, lowercase, no paths)."""
    return bool(_NAME.match(name or ""))


class WorkspaceStore:
    """Registry of name -> resolved directory, owner-edited via the CLI only."""

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

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}

    def _save(self, items: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(items, handle, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def add(self, name: str, path: str) -> str:
        """Register a directory under a name. Returns the resolved path stored."""
        if not valid_name(name):
            raise ValueError(
                f"bad workspace name {name!r}: use lowercase letters, digits, - or _ (max 32 chars)"
            )
        resolved = Path(path).resolve()
        if not resolved.is_dir():
            raise ValueError(f"not a directory: {path}")
        with self._locked():
            items = self._load()
            items[name] = str(resolved)
            self._save(items)
        return str(resolved)

    def remove(self, name: str) -> bool:
        with self._locked():
            items = self._load()
            if name not in items:
                return False
            del items[name]
            self._save(items)
            return True

    def list(self) -> dict[str, str]:
        return dict(sorted(self._load().items()))

    def resolve(self, name: str) -> Optional[str]:
        """The registered directory for a name, or None. Never invents paths."""
        return self._load().get(name)


def parse_artifact_lines(report: str) -> list[str]:
    """The relative artifact names a job's report asks to hand back, deduped."""
    seen: list[str] = []
    for match in _ARTIFACT_LINE.finditer(report or ""):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def collect_artifacts(report: str, workspace_dir: Optional[str]) -> tuple[list[str], list[str]]:
    """Resolve a report's ARTIFACT: lines to deliverable files.

    Returns ``(files, problems)``: absolute resolved paths that passed the
    containment check and the caps, and a human-readable line for every
    artifact that did not. Caps are enforced before any byte is read.
    """
    names = parse_artifact_lines(report)
    files: list[str] = []
    problems: list[str] = []
    if not names:
        return files, problems
    if not workspace_dir:
        for name in names:
            problems.append(f"artifact {name}: the job had no workspace to resolve it in")
        return files, problems

    root = Path(workspace_dir).resolve()
    total = 0
    for name in names:
        if len(files) >= ARTIFACT_MAX_FILES:
            problems.append(f"artifact {name}: skipped, over the {ARTIFACT_MAX_FILES}-file cap")
            continue
        if os.path.isabs(name) or ".." in Path(name).parts:
            problems.append(f"artifact {name}: only workspace-relative paths are allowed")
            continue
        candidate = (root / name).resolve()
        # Containment on the *resolved* path: symlinks cannot point outside.
        if root != candidate and root not in candidate.parents:
            problems.append(f"artifact {name}: resolves outside the workspace")
            continue
        if not candidate.is_file():
            problems.append(f"artifact {name}: no such file in the workspace")
            continue
        size = candidate.stat().st_size
        if total + size > ARTIFACT_MAX_BYTES:
            problems.append(
                f"artifact {name}: skipped, would exceed the "
                f"{ARTIFACT_MAX_BYTES // (1024 * 1024)} MB total cap"
            )
            continue
        total += size
        files.append(str(candidate))
    return files, problems
