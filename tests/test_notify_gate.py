"""Tests for the proactive-notify gate (pure: no I/O, no model)."""

from __future__ import annotations

from iris.notify.events import Event
from iris.notify.gate import decide, needs_model


def ev(exit_code=0, duration_s=1.0):
    return Event(source="command", kind="finished", title="job",
                 exit_code=exit_code, duration_s=duration_s)


def test_failure_always_notifies():
    assert decide(ev(exit_code=1, duration_s=0.1), 30) == "notify"


def test_quick_success_drops():
    assert decide(ev(exit_code=0, duration_s=2), 30) == "drop"


def test_long_success_notifies():
    assert decide(ev(exit_code=0, duration_s=45), 30) == "notify"


def test_always_flag_forces_notify():
    assert decide(ev(exit_code=0, duration_s=1), 30, force=True) == "notify"


def test_quiet_flag_forces_drop():
    assert decide(ev(exit_code=1, duration_s=99), 30, quiet=True) == "drop"


def test_needs_model_only_on_failure():
    assert needs_model(ev(exit_code=1)) is True
    assert needs_model(ev(exit_code=0)) is False


def test_watch_event_notifies():
    e = Event(source="watch", kind="changed", title="blog", exit_code=0, duration_s=0.0)
    assert decide(e, 30) == "notify"


def test_watch_event_quiet_drops():
    e = Event(source="watch", kind="changed", title="blog", exit_code=0, duration_s=0.0)
    assert decide(e, 30, quiet=True) == "drop"
