"""Make Claude Code skills available to the agent.

The brain is the official ``claude`` binary, which auto-loads skills from
``~/.claude/skills`` by description in headless mode. This lets you keep your
bot's skills in their own directory (``IRIS_SKILLS_DIR``) and have Iris symlink
them into the skills path at startup, instead of hand-managing ``~/.claude/skills``.

A skill is a folder with a ``SKILL.md`` whose frontmatter has a ``description``;
that is the same format Claude Code uses, so skills port directly.
"""

from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .statefile import JsonListStore

_DESCRIPTION = re.compile(r"(?mi)^description:\s*(.+)$")
# A skill folder name must be a safe slug: no path separators, no traversal, no
# surprises when it becomes a directory under the owner's skills dir.
# \Z (not $) so a trailing newline can't slip a multi-line value through.
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}\Z")


def _skills_root(claude_home: str | None = None) -> Path:
    home = Path(claude_home).expanduser() if claude_home else Path.home()
    return home / ".claude" / "skills"


def _description(skill_md: Path) -> str:
    try:
        text = skill_md.read_text("utf-8")
    except OSError:
        return ""
    match = _DESCRIPTION.search(text)
    return match.group(1).strip().strip('"').strip("'")[:140] if match else ""


def discover(claude_home: str | None = None) -> list[tuple[str, str]]:
    """List installed skills as (name, description)."""
    root = _skills_root(claude_home)
    out: list[tuple[str, str]] = []
    if not root.exists():
        return out
    for entry in sorted(root.iterdir()):
        skill_md = entry / "SKILL.md"
        if entry.is_dir() and skill_md.exists():
            out.append((entry.name, _description(skill_md)))
    return out


def link_skills(skills_dir: str, claude_home: str | None = None) -> int:
    """Symlink each skill folder in ``skills_dir`` into the skills path.

    Returns how many new links were made. Existing entries are left alone.
    """
    src = Path(skills_dir).expanduser()
    if not src.is_dir():
        return 0
    root = _skills_root(claude_home)
    root.mkdir(parents=True, exist_ok=True)
    made = 0
    for entry in src.iterdir():
        if not (entry.is_dir() and (entry / "SKILL.md").exists()):
            continue
        link = root / entry.name
        if link.exists() or link.is_symlink():
            continue
        try:
            link.symlink_to(entry.resolve())
            made += 1
        except OSError:
            pass
    return made


# -- Self-improving skills: staging + owner approval -------------------------
#
# A skill is an instruction the model follows, so letting Iris rewrite her own
# skills is the highest-stakes self-modification there is. She may PROPOSE one
# (via the maintain review or the propose_skill chat tool), but a proposal is
# only staged: it is applied to the live skills dir solely by an explicit owner
# action (`iris skills approve <id>`), never silently. The full content is stored
# so the owner reviews exactly what will run before approving.

def validate_skill(name: str, content: str) -> Optional[str]:
    """An error string if this is not a safe, well-formed skill, else None."""
    if not _SAFE_NAME.match(name or ""):
        return ("Skill name must be a lowercase slug (letters, digits, hyphens), "
                "no spaces or path separators.")
    if not _DESCRIPTION.search(content or ""):
        return "A skill needs a 'description:' line in its frontmatter."
    return None


class SkillProposalStore:
    """File-backed staging area for proposed skill changes (flock + atomic
    replace, same as the other stores). Nothing here is live until the owner
    approves it and `apply_proposal` writes it into the skills dir."""

    CAP = 50

    def __init__(self, path: str | os.PathLike[str]):
        self._store = JsonListStore(path, "skill proposals")
        self.path = self._store.path

    @contextmanager
    def _locked(self):
        with self._store.locked():
            yield

    def _load(self) -> list[dict]:
        return [item for item in self._store.load() if isinstance(item, dict)]

    def _save(self, items: list[dict]) -> None:
        self._store.save(items)

    def add(self, name: str, content: str, rationale: str, *,
            kind: str = "edit", now: Optional[float] = None) -> dict:
        now = time.time() if now is None else now
        with self._locked():
            items = self._load()
            pid = max((int(p.get("id", 0)) for p in items), default=0) + 1
            proposal = {
                "id": pid, "name": name, "content": content, "rationale": rationale,
                "kind": kind, "status": "pending", "created_ts": now,
            }
            items.append(proposal)
            if len(items) > self.CAP:
                items = items[-self.CAP:]
            self._save(items)
            return proposal

    def all(self) -> list[dict]:
        return self._load()

    def get(self, proposal_id: int) -> Optional[dict]:
        for p in self._load():
            if int(p.get("id", 0)) == int(proposal_id):
                return p
        return None

    def pending(self) -> list[dict]:
        return [p for p in self._load() if p.get("status") == "pending"]

    def transition(self, proposal_id: int, status: str, now: float) -> Optional[dict]:
        with self._locked():
            items = self._load()
            updated = None
            for p in items:
                if int(p.get("id", 0)) == int(proposal_id):
                    p["status"] = status
                    p["decided_ts"] = now
                    updated = p
                    break
            if updated is not None:
                self._save(items)
            return updated


def apply_proposal(proposal: dict, skills_dir: str, claude_home: str | None = None) -> Path:
    """Write an approved proposal into the skills dir and link it live.

    Validates again at apply time (defence in depth) and refuses to escape the
    skills dir. Returns the path to the written SKILL.md.
    """
    name, content = proposal.get("name", ""), proposal.get("content", "")
    error = validate_skill(name, content)
    if error:
        raise ValueError(error)
    root = Path(skills_dir).expanduser()
    target_dir = (root / name).resolve()
    if root.resolve() not in target_dir.parents:
        raise ValueError("skill path escapes the skills directory")
    target_dir.mkdir(parents=True, exist_ok=True)
    skill_md = target_dir / "SKILL.md"
    skill_md.write_text(content, encoding="utf-8")
    link_skills(str(root), claude_home)  # make it discoverable to claude
    return skill_md
