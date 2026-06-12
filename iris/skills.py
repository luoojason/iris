"""Make Claude Code skills available to the agent.

The brain is the official ``claude`` binary, which auto-loads skills from
``~/.claude/skills`` by description in headless mode. This lets you keep your
bot's skills in their own directory (``IRIS_SKILLS_DIR``) and have Iris symlink
them into the skills path at startup, instead of hand-managing ``~/.claude/skills``.

A skill is a folder with a ``SKILL.md`` whose frontmatter has a ``description``;
that is the same format Claude Code uses, so skills port directly.
"""

from __future__ import annotations

import re
from pathlib import Path

_DESCRIPTION = re.compile(r"(?mi)^description:\s*(.+)$")


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
