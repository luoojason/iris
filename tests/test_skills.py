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


# -- skill proposals (staging + owner approval) ------------------------------

from iris.skills import SkillProposalStore, apply_proposal, validate_skill

_GOOD = "---\nname: summarize\ndescription: Summarize a long doc into bullets\n---\nBody."


def test_validate_skill_accepts_a_well_formed_skill():
    assert validate_skill("summarize", _GOOD) is None


def test_validate_skill_rejects_unsafe_names_and_missing_description():
    assert validate_skill("../escape", _GOOD)              # path traversal
    assert validate_skill("Bad Name", _GOOD)               # spaces/caps
    assert validate_skill("ok", "no frontmatter here")     # no description


def test_validate_skill_rejects_a_trailing_newline_in_the_name():
    # a "\Z"-anchored slug, not "$" (which would allow a trailing newline through).
    assert validate_skill("summarize\n", _GOOD)
    assert validate_skill("summarize\nrm -rf", _GOOD)


def test_proposal_store_round_trip(tmp_path):
    store = SkillProposalStore(tmp_path / "p.json")
    p = store.add("summarize", _GOOD, "I keep being asked to summarize", kind="new", now=1.0)
    assert p["status"] == "pending" and p["name"] == "summarize"
    assert [x["id"] for x in store.pending()] == [p["id"]]
    store.transition(p["id"], "rejected", now=2.0)
    assert store.pending() == []
    assert SkillProposalStore(tmp_path / "p.json").get(p["id"])["status"] == "rejected"


def test_apply_proposal_writes_and_links_the_skill(tmp_path):
    home = tmp_path / "home"
    skills_dir = tmp_path / "myskills"
    store = SkillProposalStore(tmp_path / "p.json")
    p = store.add("summarize", _GOOD, "rationale", kind="new", now=1.0)
    path = apply_proposal(p, str(skills_dir), claude_home=str(home))
    assert path.read_text("utf-8") == _GOOD
    # it is now discoverable as a live skill
    assert dict(discover(str(home))).get("summarize") == "Summarize a long doc into bullets"


def test_apply_proposal_refuses_an_invalid_proposal(tmp_path):
    store = SkillProposalStore(tmp_path / "p.json")
    bad = store.add("../escape", _GOOD, "r", kind="new", now=1.0)
    try:
        apply_proposal(bad, str(tmp_path / "myskills"), claude_home=str(tmp_path / "home"))
        assert False, "expected ValueError"
    except ValueError:
        pass
