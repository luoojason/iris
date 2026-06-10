"""Owner-bound repo workspaces: the names jobs may run inside.

The security model (see the repo-workspaces design spec): the model NEVER
names a filesystem path. A job requests a workspace by NAME, and this store
is the only place a name resolves to a path. Names are bound exclusively by
the owner from a local shell (``iris workspaces add NAME PATH``), so a
hostile prompt can only point a job at checkouts the owner already blessed.

Storage is a small JSON file, ``name -> {path, added_at}``, with the same
atomic-write and corrupt-tolerant discipline as :class:`iris.sessions.SessionStore`.
The path is a constructor argument; config wiring (IRIS_WORKSPACES_FILE)
happens at the call sites, never here.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Optional

# The default registry filename; resolved against env/config by the callers.
DEFAULT_WORKSPACES_FILE = "iris-workspaces.json"

# Workspace names are deliberately tame: lowercase slugs only, so they are
# unambiguous in chat, in argv, and in error messages, and can never smuggle
# path syntax (slashes, dots, whitespace) into the registry.
# fullmatch (not match-with-$): $ would tolerate a trailing newline.
_NAME = re.compile(r"[a-z0-9-]{1,32}")


class WorkspaceStore:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    def _load(self) -> dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self, data: dict[str, dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def add(self, name: str, path: str) -> dict:
        """Bind ``name`` to an existing directory; returns the stored entry.

        The path is resolved to an absolute path before storing, so a later
        job runs against a stable location no matter where the CLI was
        invoked from. Rebinding an existing name updates it in place.
        """
        if not _NAME.fullmatch(name or ""):
            raise ValueError(
                f"invalid workspace name {name!r}: use 1-32 characters of a-z, 0-9, or '-'"
            )
        resolved = Path(path).expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(
                f"workspace path {path!r} is not an existing directory"
            )
        data = self._load()
        entry = {"path": str(resolved), "added_at": time.time()}
        data[name] = entry
        self._save(data)
        return entry

    def remove(self, name: str) -> bool:
        data = self._load()
        if name not in data:
            return False
        del data[name]
        self._save(data)
        return True

    def get(self, name: str) -> Optional[dict]:
        return self._load().get(name)

    def all(self) -> dict[str, dict]:
        return dict(sorted(self._load().items()))
