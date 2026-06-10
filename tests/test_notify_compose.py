"""Tests for the notify composer (template, or one model call on failure)."""

from __future__ import annotations

from iris.notify import compose
from iris.notify.events import Event


class FakeResult:
    def __init__(self, text, is_error=False):
        self.text = text
        self.is_error = is_error


class FakeDriver:
    def __init__(self, result):
        self._result = result
        self.calls = []

    def run(self, prompt, session_id=None, model=None):
        self.calls.append(prompt)
        return self._result


def ev(exit_code=0, duration_s=134.0, tail=""):
    return Event(source="command", kind="finished", title="npm test",
                 exit_code=exit_code, duration_s=duration_s, tail=tail)


def test_success_uses_template_no_model():
    assert compose.render(ev(exit_code=0, duration_s=134), None) == "done: npm test passed in 2m14s"


def test_failure_template_when_no_driver():
    assert compose.render(ev(exit_code=1, duration_s=40), None) == "failed: npm test exited 1 after 40s"


def test_failure_uses_model_and_passes_tail():
    driver = FakeDriver(FakeResult("Looks like the DB migration failed. Want me to look?"))
    out = compose.render(ev(exit_code=1, tail="ERROR: relation does not exist"), driver)
    assert "DB migration" in out
    assert "ERROR: relation does not exist" in driver.calls[0]  # tail reached the prompt


def test_model_error_falls_back_to_template():
    driver = FakeDriver(FakeResult("", is_error=True))
    assert compose.render(ev(exit_code=1, duration_s=40), driver) == "failed: npm test exited 1 after 40s"


def test_watch_change_template():
    e = Event(source="watch", kind="changed", title="api-version",
              exit_code=0, duration_s=0.0, tail="4.1", detail="4.2")
    assert compose.render(e, None) == "changed: api-version is now 4.2 (was 4.1)"


def test_watch_change_truncates_long_values():
    e = Event(source="watch", kind="changed", title="page",
              exit_code=0, duration_s=0.0, tail="old", detail="x" * 300)
    out = compose.render(e, None)
    assert out.startswith("changed: page is now ")
    assert "..." in out
    assert len(out) < 200
