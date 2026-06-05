"""Tests for the transport-agnostic agent core."""

from __future__ import annotations

from iris.agent import Agent
from iris.driver import ClaudeResult
from iris.sessions import SessionStore


class FakeDriver:
    """Records calls and returns queued results."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def run(self, prompt, session_id=None):
        self.calls.append((prompt, session_id))
        return self.results.pop(0)


def test_respond_persists_new_session(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    driver = FakeDriver([ClaudeResult(text="hi", session_id="sess-1", is_error=False)])
    agent = Agent(driver, store)
    result = agent.respond("c1", "hello")
    assert result.text == "hi"
    assert store.get("c1") == "sess-1"


def test_respond_resumes_existing_session(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    store.set("c1", "old-session")
    driver = FakeDriver([ClaudeResult(text="ok", session_id="old-session", is_error=False)])
    agent = Agent(driver, store)
    agent.respond("c1", "again")
    # the driver was asked to resume the stored session
    assert driver.calls[0] == ("again", "old-session")


def test_error_result_does_not_clobber_session(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    store.set("c1", "keep-me")
    driver = FakeDriver([ClaudeResult(text="", session_id=None, is_error=True, error="boom")])
    agent = Agent(driver, store)
    result = agent.respond("c1", "x")
    assert result.is_error
    assert store.get("c1") == "keep-me"  # unchanged when no new session id


def test_reset_clears_session(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    store.set("c1", "sess")
    agent = Agent(FakeDriver([]), store)
    assert agent.reset("c1") is True
    assert store.get("c1") is None


def test_dead_session_is_healed_and_retried(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    store.set("c1", "dead-id")
    driver = FakeDriver([
        ClaudeResult(text="", session_id=None, is_error=True, error="No conversation found for dead-id"),
        ClaudeResult(text="fresh", session_id="new-id", is_error=False),
    ])
    agent = Agent(driver, store)
    result = agent.respond("c1", "hi")
    assert result.text == "fresh"
    assert driver.calls[0] == ("hi", "dead-id")  # tried the dead id
    assert driver.calls[1] == ("hi", None)        # then retried fresh
    assert store.get("c1") == "new-id"


def test_other_error_does_not_drop_the_session(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    store.set("c1", "keep-me")
    driver = FakeDriver([ClaudeResult(text="", session_id=None, is_error=True, error="rate_limit_error")])
    agent = Agent(driver, store)
    result = agent.respond("c1", "hi")
    assert result.is_error
    assert len(driver.calls) == 1        # not retried
    assert store.get("c1") == "keep-me"  # session preserved


def test_respond_serializes_same_conversation(tmp_path):
    import threading
    import time

    store = SessionStore(tmp_path / "s.json")
    state = {"current": 0, "max": 0}
    guard = threading.Lock()

    class SlowDriver:
        def run(self, prompt, session_id=None):
            with guard:
                state["current"] += 1
                state["max"] = max(state["max"], state["current"])
            time.sleep(0.03)
            with guard:
                state["current"] -= 1
            return ClaudeResult(text="ok", session_id="s", is_error=False)

    agent = Agent(SlowDriver(), store)
    threads = [threading.Thread(target=agent.respond, args=("c1", "hi")) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["max"] == 1  # never two turns at once for the same conversation


def test_from_config_builds_agent(tmp_path):
    from iris.config import Config
    cfg = Config(session_store_path=str(tmp_path / "s.json"), model="claude-sonnet-4-6")
    agent = Agent.from_config(cfg)
    assert agent.driver.model == "claude-sonnet-4-6"
    assert agent.driver.append_system_prompt_file == cfg.persona_file
    assert isinstance(agent.store, SessionStore)
