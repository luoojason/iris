"""Tests for skill discovery and linking."""

from __future__ import annotations

from iris.skills import discover, link_skills


def _make_skill(dirpath, name, description):
    skill = dirpath / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\nbody here", encoding="utf-8"
    )
    return skill


def test_discover_reads_name_and_description(tmp_path):
    home = tmp_path / "home"
    root = home / ".claude" / "skills"
    _make_skill(root, "weather", "Get the current weather")
    _make_skill(root, "notes", "Take quick notes")
    (root / "not-a-skill").mkdir()  # no SKILL.md -> ignored
    found = dict(discover(str(home)))
    assert found["weather"] == "Get the current weather"
    assert found["notes"] == "Take quick notes"
    assert "not-a-skill" not in found


def test_link_skills_makes_them_discoverable_and_is_idempotent(tmp_path):
    home = tmp_path / "home"
    src = tmp_path / "myskills"
    _make_skill(src, "translate", "Translate text between languages")
    assert link_skills(str(src), str(home)) == 1
    assert dict(discover(str(home))).get("translate") == "Translate text between languages"
    assert link_skills(str(src), str(home)) == 0  # already linked


def test_link_skills_missing_source_is_zero(tmp_path):
    assert link_skills(str(tmp_path / "nope"), str(tmp_path / "home")) == 0
