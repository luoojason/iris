"""Pure formatters for the per-turn runtime footer and the context view.

Nothing here does I/O or touches the model. Both functions take plain data the
driver already returns (a :class:`~iris.driver.ClaudeResult`-like object for the
footer, raw counts for the context view) and return a string to show in chat:

* :func:`format_footer` is the one-line "· sonnet · 47k ctx · 1.2s" stamp an
  adapter can append under a reply when the ``!footer on`` toggle is set.
* :func:`format_context` is the short multi-line fullness view behind an
  ``iris context`` subcommand: how full the window is and how close compaction is.

Keeping them pure means the adapter decides when to call them and the tests need
nothing more than a plain object with the right attributes.
"""

from __future__ import annotations

from typing import Optional

# The middle dot that separates footer fields, also used as the leading marker.
_SEP = "·"


def _fmt_ktokens(tokens: int) -> str:
    """Render a token count compactly: 47000 -> "47k", 512 -> "512".

    Counts at or above a thousand collapse to rounded k-tokens (how these
    numbers are read in practice); smaller counts stay exact so a near-empty
    window does not misleadingly show "0k".
    """
    tokens = int(tokens)
    if abs(tokens) >= 1000:
        return f"{round(tokens / 1000)}k"
    return str(tokens)


def _short_model(model: str) -> str:
    """Collapse a full model id to its family word for the footer.

    "claude-sonnet-4-5-20250929" -> "sonnet". An unrecognized string is
    returned unchanged so a custom or future model name still shows something.
    """
    low = model.lower()
    for family in ("opus", "sonnet", "haiku"):
        if family in low:
            return family
    return model


def format_footer(result, *, show_cost: bool = False) -> str:
    """One compact status line for a finished turn, e.g. ``· sonnet · 47k ctx · 1.2s``.

    ``result`` is any object with the ``ClaudeResult`` attributes (model,
    context_tokens, duration_ms, cost_usd); missing or ``None`` attributes are
    simply left out. Cost is shown only when ``show_cost`` is set and the datum
    is present, because subscription OAuth turns usually report ``cost_usd`` as
    ``None``. With no field carrying data the line collapses to ``""`` so the
    caller can skip it entirely.
    """
    parts: list[str] = []

    model = getattr(result, "model", None)
    if model:
        parts.append(_short_model(model))

    context_tokens = getattr(result, "context_tokens", None)
    if context_tokens is not None:
        parts.append(f"{_fmt_ktokens(context_tokens)} ctx")

    duration_ms = getattr(result, "duration_ms", None)
    if duration_ms is not None:
        parts.append(f"{duration_ms / 1000:.1f}s")

    cost_usd = getattr(result, "cost_usd", None)
    if show_cost and cost_usd is not None:
        parts.append(f"${cost_usd:.3f}")

    if not parts:
        return ""
    return f"{_SEP} " + f" {_SEP} ".join(parts)


def format_context(
    context_tokens: Optional[int],
    compact_at_tokens: int,
    turns: int,
    compact_every: int,
) -> str:
    """Short multi-line view of how full the window is and how near compaction is.

    Line one is the token count, shown against the compaction threshold with a
    percentage when ``compact_at_tokens`` is positive; a zero (or negative)
    threshold drops the ratio and percentage so there is never a divide by zero.
    Line two is turns taken and turns left until the turn-count backstop fires;
    a ``compact_every`` of ``0`` means that backstop is off, so it reads
    "no turn-based compaction" instead.
    """
    tokens = int(context_tokens or 0)
    turns = int(turns)

    if compact_at_tokens > 0:
        pct = round(100 * tokens / compact_at_tokens)
        token_line = (
            f"context: {_fmt_ktokens(tokens)} / {_fmt_ktokens(compact_at_tokens)} "
            f"tokens ({pct}% to compaction)"
        )
    else:
        token_line = f"context: {_fmt_ktokens(tokens)} tokens"

    if compact_every > 0:
        remaining = compact_every - turns
        if remaining > 0:
            turn_line = f"turns: {turns} done, compaction in {remaining}"
        else:
            turn_line = f"turns: {turns} done, compaction due"
    else:
        turn_line = f"turns: {turns} done, no turn-based compaction"

    return f"{token_line}\n{turn_line}"
