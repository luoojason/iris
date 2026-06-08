"""Pick a model per turn, to stretch the subscription's agent credit.

Running every message on the strongest model is simplest and safest, and is the
default. But a lot of chat traffic is trivial ("thanks", "lol", "what time is
it"), and spending Opus on those is wasteful. When routing is enabled, Iris
sends only *clearly* trivial turns to a lighter model and everything else to the
default model.

The bias is deliberate: routing down is the risky direction (a hard question
answered by a weak model is a bad reply), so the heuristic only downgrades short,
keyword-free, attachment-free messages. When in any doubt it keeps the strong
model. This is a pure function so it is easy to test and reason about; it makes
no model call of its own (that would defeat the point).
"""

from __future__ import annotations

from typing import Optional

# Stems that signal a turn wants real reasoning, code, or analysis. Their
# presence forces the strong model regardless of length. These are matched as
# substrings, so stems (not whole words) are used on purpose: "analyz" catches
# analyze / analyse / analyzing / analyzed, which the whole-word forms missed.
# Over-keeping the strong model is the cheap mistake; downgrading a hard turn to
# a weak model is the costly one, so the list errs toward catching work.
_HEAVY_HINTS = (
    "analyz", "analys", "debug", "explain", "explanation", "why", "how come",
    "plan", "design", "prove", "proof", "deriv", "compar", "refactor",
    "implement", "optim", "error", "exception", "traceback", "stack trace",
    "bug", "fix", "review", "summar", "translat", "calculat", "estimat",
    "research", "diagnos", "troubleshoot", "evaluat", "rank",
    # common imperative work verbs that short generation/solve turns use
    "write", "rewrite", "create", "build", "generate", "draft", "compose",
    "solve", "code", "script", "function", "convert", "outline",
)


def choose_model_explained(
    text: str,
    *,
    light_model: Optional[str],
    has_attachments: bool = False,
    trivial_max_chars: int = 140,
) -> tuple[Optional[str], str]:
    """Like :func:`choose_model`, but also return a short reason string.

    Returns ``(light_model, "trivial")`` for a clearly-trivial turn, otherwise
    ``(None, <reason>)`` where reason names the gate that forced the strong model
    (or ``"light-disabled"`` when no light model is configured).
    """
    if not light_model:
        return None, "light-disabled"
    if has_attachments:
        return None, "has-attachments"  # an image or voice note deserves the strong model
    stripped = text.strip()
    if len(stripped) > trivial_max_chars:
        return None, "too-long"
    if "```" in text:
        return None, "code-fence"
    if ("?" in text or "？" in text) and len(stripped) > 60:  # full-width ？ counts too
        return None, "long-question"
    lowered = stripped.lower()
    for hint in _HEAVY_HINTS:
        if hint in lowered:
            return None, f"heavy-hint:{hint}"
    return light_model, "trivial"


def choose_model(
    text: str,
    *,
    light_model: Optional[str],
    has_attachments: bool = False,
    trivial_max_chars: int = 140,
) -> Optional[str]:
    """Return ``light_model`` for a clearly-trivial turn, else ``None``.

    ``None`` means "use the driver's default (strong) model". Thin wrapper over
    :func:`choose_model_explained` that drops the reason, for back-compat.
    """
    model, _ = choose_model_explained(
        text,
        light_model=light_model,
        has_attachments=has_attachments,
        trivial_max_chars=trivial_max_chars,
    )
    return model
