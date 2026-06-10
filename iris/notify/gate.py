"""Decide whether an event is worth a ping, and whether it needs the model.

Pure functions, no I/O and no model call: this is the noise-and-cost control
point. ``fold`` is reserved for the future briefing and is not emitted yet.
"""

from __future__ import annotations

from .events import Event


def decide(event: Event, min_seconds: float, force: bool = False, quiet: bool = False) -> str:
    """Return "notify" or "drop" for this event."""
    if quiet:
        return "drop"
    if event.source == "watch":
        return "notify"  # a watch event only exists when its value actually changed
    if force:
        return "notify"
    if event.exit_code != 0:
        return "notify"
    if event.duration_s >= min_seconds:
        return "notify"
    return "drop"


def needs_model(event: Event) -> bool:
    """True when the event carries judgment worth one model call (a failure)."""
    return event.exit_code != 0
