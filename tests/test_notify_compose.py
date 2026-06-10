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


def job_ev(exit_code=0, duration_s=134.0, tail="", kind="finished"):
    return Event(source="job", kind=kind, title="refactor parser",
                 exit_code=exit_code, duration_s=duration_s, tail=tail)


def test_job_success_uses_job_done_template():
    assert compose.render(job_ev(exit_code=0, duration_s=134), None) == "job done: refactor parser in 2m14s"


def test_job_failure_template_when_no_driver():
    assert compose.render(job_ev(exit_code=2, duration_s=40), None) == "job failed: refactor parser exited 2 after 40s"


def test_job_failure_selects_job_prompt_with_details():
    driver = FakeDriver(FakeResult("The parser refactor hit a type error. Want me to dig in?"))
    out = compose.render(job_ev(exit_code=1, duration_s=40, tail="TypeError: cannot unpack"), driver)
    assert out == "The parser refactor hit a type error. Want me to dig in?"
    prompt = driver.calls[0]
    assert "background job" in prompt
    assert "refactor parser" in prompt
    assert "Exit code: 1" in prompt
    assert "40s" in prompt
    assert "TypeError: cannot unpack" in prompt


def test_command_failure_still_gets_command_prompt_not_job_prompt():
    driver = FakeDriver(FakeResult("Migration broke."))
    compose.render(ev(exit_code=1, tail="boom"), driver)
    prompt = driver.calls[0]
    assert "A command the user was running just failed" in prompt
    assert "background job" not in prompt


def test_job_success_skips_model_even_with_driver():
    driver = FakeDriver(FakeResult("should never be used"))
    assert compose.render(job_ev(exit_code=0, duration_s=134), driver) == "job done: refactor parser in 2m14s"
    assert driver.calls == []


def test_job_model_error_falls_back_to_job_template():
    driver = FakeDriver(FakeResult("", is_error=True))
    assert compose.render(job_ev(exit_code=1, duration_s=40), driver) == "job failed: refactor parser exited 1 after 40s"


def test_job_model_exception_falls_back_to_job_template():
    class RaisingDriver:
        def run(self, prompt, session_id=None, model=None):
            raise RuntimeError("driver blew up")

    assert compose.render(job_ev(exit_code=1, duration_s=40), RaisingDriver()) == "job failed: refactor parser exited 1 after 40s"


def test_job_empty_model_text_falls_back_to_job_template():
    driver = FakeDriver(FakeResult("   "))
    assert compose.render(job_ev(exit_code=1, duration_s=40), driver) == "job failed: refactor parser exited 1 after 40s"


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
