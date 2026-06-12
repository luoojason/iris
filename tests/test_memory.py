"""Tests for the pure memory ranking core (no I/O, no model)."""

from __future__ import annotations

from datetime import datetime, timezone

from iris.memory import normalize, rank, relevance, score

NOW = datetime(2026, 6, 7, tzinfo=timezone.utc).timestamp()


def _note(id, text, tags=None, **extra):
    e = {"id": id, "text": text, "tags": tags or [], "created_at": "2026-06-01T00:00:00Z"}
    e.update(extra)
    return e


def test_normalize_fills_legacy_note():
    # A note from before importance/pinned/use_count existed still loads.
    norm = normalize({"id": 1, "text": "hi", "tags": "a, b"})
    assert norm["importance"] == 3
    assert norm["pinned"] is False
    assert norm["use_count"] == 0
    assert norm["tags"] == ["a", "b"]


def test_normalize_clamps_importance():
    assert normalize({"importance": 99})["importance"] == 5
    assert normalize({"importance": 0})["importance"] == 1
    assert normalize({"importance": "bad"})["importance"] == 3


def test_relevance_counts_distinct_query_terms():
    note = _note(1, "Jason likes terse replies", tags=["style"])
    assert relevance(note, ["terse", "replies"]) == 2
    assert relevance(note, ["terse", "terse"]) == 1  # distinct terms only
    assert relevance(note, ["style"]) == 1  # tags count
    assert relevance(note, ["unrelated"]) == 0


def test_query_drops_irrelevant_unpinned_notes():
    hit = _note(1, "quant finance research")
    miss = _note(2, "favorite color is blue")
    ranked = rank([hit, miss], "finance", NOW)
    assert [n["id"] for n in ranked] == [1]


def test_pinned_note_survives_and_leads_even_without_match():
    pinned = _note(1, "totally unrelated", pinned=True)
    match = _note(2, "finance stuff")
    ranked = rank([pinned, match], "finance", NOW)
    # pinned is never dropped, and its 1000 floor puts it first
    assert ranked[0]["id"] == 1
    assert {n["id"] for n in ranked} == {1, 2}


def test_relevance_beats_importance_when_querying():
    # A weak-importance exact match outranks a high-importance non-match.
    strong_irrelevant = _note(1, "GIS mapping notes", importance=5)
    weak_relevant = _note(2, "finance finance", importance=1)
    ranked = rank([strong_irrelevant, weak_relevant], "finance", NOW)
    assert ranked[0]["id"] == 2


def test_importance_orders_a_browse_with_no_query():
    low = _note(1, "trivia", importance=1)
    high = _note(2, "key fact", importance=5)
    ranked = rank([low, high], None, NOW)
    assert [n["id"] for n in ranked] == [2, 1]


def test_mark_useful_breaks_ties_but_does_not_dominate():
    base = _note(1, "finance one")
    used = _note(2, "finance two", use_count=12)
    ranked = rank([base, used], "finance", NOW)
    # equal relevance + importance, so the used note edges ahead
    assert ranked[0]["id"] == 2
    # but usefulness can never overtake a stronger relevance match (one that
    # shares an extra *distinct* query term; repeating a word does not count)
    better_match = _note(3, "finance markets unused")
    ranked2 = rank([used, better_match], "finance markets", NOW)
    assert ranked2[0]["id"] == 3


def test_use_count_cap_keeps_signal_weak():
    # A massively-used note still cannot outscore one extra relevance hit.
    spammed = _note(1, "finance", use_count=10_000)
    two_hits = _note(2, "finance markets")
    ranked = rank([spammed, two_hits], "finance markets", NOW)
    assert ranked[0]["id"] == 2


def test_rank_respects_limit_and_is_deterministic():
    notes = [_note(i, f"finance note {i}") for i in range(5)]
    ranked = rank(notes, "finance", NOW, limit=2)
    assert len(ranked) == 2
    # identical scores: newest id wins the tiebreak, deterministically
    assert [n["id"] for n in ranked] == [4, 3]


def test_score_returns_none_for_dropped_note():
    assert score(_note(1, "blue"), ["finance"], NOW) is None
    assert score(_note(1, "blue"), [], NOW) is not None  # no query, kept


# -- pinned digest (the always-loaded tier) -----------------------------------


def _pnote(nid, text, pinned=False, importance=3):
    return {"id": nid, "text": text, "pinned": pinned, "importance": importance,
            "created_at": "2026-06-01T00:00:00Z"}


def test_pinned_digest_renders_only_pinned():
    from iris.memory import pinned_digest

    entries = [_pnote(1, "owner prefers metric", pinned=True),
               _pnote(2, "passing chatter about lunch")]
    out = pinned_digest(entries, now_ts=1.75e9)
    assert "owner prefers metric" in out
    assert "lunch" not in out


def test_pinned_digest_empty_when_nothing_pinned():
    from iris.memory import pinned_digest

    assert pinned_digest([_pnote(1, "unpinned")], now_ts=1.75e9) == ""
    assert pinned_digest([], now_ts=1.75e9) == ""


def test_pinned_digest_orders_by_importance():
    from iris.memory import pinned_digest

    entries = [_pnote(1, "minor fact", pinned=True, importance=1),
               _pnote(2, "major fact", pinned=True, importance=5)]
    out = pinned_digest(entries, now_ts=1.75e9)
    assert out.index("major fact") < out.index("minor fact")


def test_pinned_digest_respects_byte_budget():
    from iris.memory import pinned_digest

    big = _pnote(1, "x" * 5000, pinned=True, importance=5)
    small = _pnote(2, "small pinned fact", pinned=True, importance=1)
    out = pinned_digest([big, small], now_ts=1.75e9, max_bytes=300)
    # the oversize note is skipped whole; the small one still fits
    assert "small pinned fact" in out
    assert "xxxx" not in out
    assert len(out.encode("utf-8")) <= 300


def test_pinned_digest_zero_budget_is_off():
    from iris.memory import pinned_digest

    assert pinned_digest([_pnote(1, "fact", pinned=True)], now_ts=1.75e9, max_bytes=0) == ""
