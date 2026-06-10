"""Turn an event into the message Iris sends.

Routine events are a free templated line. Only when the gate says the event
needs the model (a failure) and a driver is provided do we spend one one-shot
call to read the output in Iris's voice. The model never blocks a notification:
any error falls back to the template.
"""

from __future__ import annotations

from .events import Event
from .gate import needs_model


def _fmt(seconds: float) -> str:
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    return f"{total // 60}m{total % 60:02d}s"


def _short(text: str, limit: int = 120) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def _template(event: Event) -> str:
    if event.source == "watch":
        return f"changed: {event.title} is now {_short(event.detail)} (was {_short(event.tail)})"
    if event.exit_code == 0:
        return f"done: {event.title} passed in {_fmt(event.duration_s)}"
    return f"failed: {event.title} exited {event.exit_code} after {_fmt(event.duration_s)}"


def _failure_prompt(event: Event) -> str:
    return (
        "A command the user was running just failed. In your own voice, in one or "
        "two short sentences, tell them what likely went wrong and offer to look "
        "closer. Be specific if the output makes the cause clear.\n\n"
        f"Command: {event.title}\n"
        f"Exit code: {event.exit_code}\n"
        f"Duration: {_fmt(event.duration_s)}\n"
        f"Last output:\n{event.tail or '(no output captured)'}"
    )


def render(event: Event, driver) -> str:
    """Return the message text. ``driver`` is None for routine events."""
    if driver is None or not needs_model(event):
        return _template(event)
    try:
        result = driver.run(_failure_prompt(event), session_id=None)
    except Exception:
        return _template(event)
    if getattr(result, "is_error", True) or not (getattr(result, "text", "") or "").strip():
        return _template(event)
    return result.text.strip()
