"""Tests for the pure runtime-footer and context-view formatters."""

from __future__ import annotations

from types import SimpleNamespace

from iris.driver import ClaudeResult
from iris.footer import format_context, format_footer


def _result(**overrides):
    """A ClaudeResult-like object with sensible footer fields, overridable."""
    base = dict(
        model="claude-sonnet-4-5-20250929",
        context_tokens=47000,
        duration_ms=1200,
        cost_usd=0.012,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# -- format_footer -----------------------------------------------------------


def test_footer_compact_line_matches_example():
    assert format_footer(_result()) == "· sonnet · 47k ctx · 1.2s"


def test_footer_shortens_full_model_id_to_family():
    assert format_footer(_result(model="claude-opus-4-8")).startswith("· opus ·")
    assert format_footer(_result(model="claude-3-5-haiku-20241022")).startswith("· haiku ·")


def test_footer_keeps_unrecognized_model_verbatim():
    assert format_footer(_result(model="gpt-fake", context_tokens=None,
                                 duration_ms=None)) == "· gpt-fake"


def test_footer_omits_cost_by_default():
    line = format_footer(_result())
    assert "$" not in line


def test_footer_includes_cost_when_requested():
    line = format_footer(_result(cost_usd=0.012), show_cost=True)
    assert line == "· sonnet · 47k ctx · 1.2s · $0.012"


def test_footer_omits_cost_when_none_even_if_requested():
    # Subscription OAuth turns commonly report no cost.
    line = format_footer(_result(cost_usd=None), show_cost=True)
    assert "$" not in line
    assert line == "· sonnet · 47k ctx · 1.2s"


def test_footer_omits_missing_fields():
    line = format_footer(SimpleNamespace(model="claude-sonnet-4-5",
                                         context_tokens=None,
                                         duration_ms=None,
                                         cost_usd=None))
    assert line == "· sonnet"


def test_footer_empty_when_no_field_has_data():
    blank = SimpleNamespace(model=None, context_tokens=None,
                            duration_ms=None, cost_usd=None)
    assert format_footer(blank) == ""


def test_footer_handles_object_missing_attributes_entirely():
    # An object without the attributes at all must not raise.
    assert format_footer(SimpleNamespace()) == ""


def test_footer_ktokens_rounds_and_keeps_small_counts_exact():
    assert format_footer(_result(context_tokens=47500, model=None,
                                 duration_ms=None)) == "· 48k ctx"
    assert format_footer(_result(context_tokens=512, model=None,
                                 duration_ms=None)) == "· 512 ctx"
    assert format_footer(_result(context_tokens=0, model=None,
                                 duration_ms=None)) == "· 0 ctx"


def test_footer_duration_is_one_decimal_seconds():
    assert format_footer(_result(model=None, context_tokens=None,
                                 duration_ms=65000)) == "· 65.0s"


def test_footer_works_on_real_claude_result():
    result = ClaudeResult(
        text="hi", session_id="s1", is_error=False,
        model="claude-sonnet-4-5-20250929", context_tokens=47000, duration_ms=1200,
    )
    assert format_footer(result) == "· sonnet · 47k ctx · 1.2s"


# -- format_context ----------------------------------------------------------


def test_context_shows_tokens_threshold_and_percent():
    out = format_context(47000, 150000, turns=12, compact_every=60)
    lines = out.splitlines()
    assert lines[0] == "context: 47k / 150k tokens (31% to compaction)"
    assert lines[1] == "turns: 12 done, compaction in 48"


def test_context_omits_percent_when_threshold_is_zero():
    out = format_context(47000, 0, turns=12, compact_every=60)
    first = out.splitlines()[0]
    assert first == "context: 47k tokens"
    assert "%" not in first


def test_context_never_divides_by_zero_on_zero_threshold():
    # Guard: a zero threshold must not raise.
    out = format_context(0, 0, turns=0, compact_every=0)
    assert out.splitlines()[0] == "context: 0 tokens"


def test_context_turn_backstop_off_when_compact_every_zero():
    out = format_context(47000, 150000, turns=12, compact_every=0)
    assert out.splitlines()[1] == "turns: 12 done, no turn-based compaction"


def test_context_reports_compaction_due_at_or_past_turn_limit():
    out = format_context(47000, 150000, turns=60, compact_every=60)
    assert out.splitlines()[1] == "turns: 60 done, compaction due"
    past = format_context(47000, 150000, turns=75, compact_every=60)
    assert past.splitlines()[1] == "turns: 75 done, compaction due"


def test_context_coerces_none_tokens_to_zero():
    out = format_context(None, 150000, turns=3, compact_every=60)
    assert out.splitlines()[0] == "context: 0 / 150k tokens (0% to compaction)"


def test_context_percent_can_exceed_one_hundred_when_over_threshold():
    out = format_context(180000, 150000, turns=5, compact_every=60)
    assert "(120% to compaction)" in out.splitlines()[0]


def test_context_is_two_lines():
    out = format_context(47000, 150000, turns=12, compact_every=60)
    assert len(out.splitlines()) == 2
