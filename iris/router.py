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

# Words that signal a turn wants real reasoning, code, or analysis. Their
# presence forces the strong model regardless of length.
_HEAVY_HINTS = (
    "analyze", "analyse", "debug", "explain", "why", "how come", "plan", "design",
    "prove", "derive", "compare", "refactor", "implement", "optimize", "optimise",
    "error", "exception", "traceback", "stack trace", "bug", "fix", "review",
    "summarize", "summarise", "translate", "calculate", "estimate", "research",
)


def choose_model(
    text: str,
    *,
    light_model: Optional[str],
    has_attachments: bool = False,
    trivial_max_chars: int = 140,
) -> Optional[str]:
    """Return ``light_model`` for a clearly-trivial turn, else ``None``.

    ``None`` means "use the driver's default (strong) model". The router only
    ever downgrades: a turn is sent to ``light_model`` only when it is short,
    carries no attachment, has no code fence, and contains none of the "this
    needs thinking" hints. Anything else (and the no-light-model case) returns
    ``None``, so the default model is used.
    """
    if not light_model:
        return None
    if has_attachments:
        return None  # an image or voice note deserves the strong model
    stripped = text.strip()
    if len(stripped) > trivial_max_chars:
        return None
    if "```" in text or ("?" in text and len(stripped) > 60):
        return None
    lowered = stripped.lower()
    if any(hint in lowered for hint in _HEAVY_HINTS):
        return None
    return light_model
