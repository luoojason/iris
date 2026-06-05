"""Tests for the transport-agnostic agent core."""

from __future__ import annotations

from iris.agent import Agent
from iris.driver import ClaudeResult
from iris.sessions import SessionStore


class FakeDriver:
    """Records calls and returns queued results."""

    def __init__(self, results, model=None):
        self.results = list(results)
        self.calls = []
        self.model = model  # the driver's default model, read by the router

    def run(self, prompt, session_id=None, model=None):
        self.calls.append((prompt, session_id))
        self.model_calls = getattr(self, "model_calls", [])
        self.model_calls.append(model)
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
        model = None

        def run(self, prompt, session_id=None, model=None):
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


def test_overflow_heals_to_fresh_session(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    store.set("c1", "big-session")
    driver = FakeDriver([
        ClaudeResult(text="", session_id=None, is_error=True, error="prompt is too long"),
        ClaudeResult(text="recovered", session_id="new-id", is_error=False),
    ])
    agent = Agent(driver, store)
    result = agent.respond("c1", "hi")
    assert result.text == "recovered"
    assert driver.calls[0] == ("hi", "big-session")  # tried the overgrown id
    assert driver.calls[1] == ("hi", None)           # then retried fresh
    assert store.get("c1") == "new-id"


def test_compaction_summarizes_and_reseeds(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    driver = FakeDriver([
        ClaudeResult(text="reply 1", session_id="s1", is_error=False),
        ClaudeResult(text="reply 2", session_id="s1", is_error=False),
        ClaudeResult(text="SUMMARY OF CHAT", session_id="s1", is_error=False),  # summary turn
        ClaudeResult(text="ok", session_id="s2", is_error=False),               # fresh seeded session
    ])
    agent = Agent(driver, store, compact_every=2)
    agent.compact_async = False  # run compaction inline for a deterministic test

    agent.respond("c1", "first")
    assert store.turns("c1") == 1  # no compaction yet
    agent.respond("c1", "second")  # second turn crosses the threshold

    # The old session was asked to summarize, and a fresh session was seeded.
    prompts = [p for p, _ in driver.calls]
    assert any("Summarize our entire conversation" in p for p in prompts)
    assert any("SUMMARY OF CHAT" in p for p in prompts)  # the summary seeded the new one
    assert store.get("c1") == "s2"  # now on the fresh session
    assert store.turns("c1") == 1   # counter reset for the new session


def test_compaction_triggers_on_context_tokens(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    driver = FakeDriver([
        # One heavy turn whose context is already near the window.
        ClaudeResult(text="reply", session_id="s1", is_error=False, context_tokens=160000),
        ClaudeResult(text="SUMMARY", session_id="s1", is_error=False),
        ClaudeResult(text="ok", session_id="s2", is_error=False),
    ])
    agent = Agent(driver, store, compact_every=0, compact_at_tokens=150000)
    agent.compact_async = False
    agent.respond("c1", "fetch a huge page")  # one turn is enough to cross the token line
    assert store.get("c1") == "s2"  # compacted despite only one turn


def test_no_compaction_below_token_threshold(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    driver = FakeDriver([ClaudeResult(text="reply", session_id="s1", is_error=False, context_tokens=40000)])
    agent = Agent(driver, store, compact_every=0, compact_at_tokens=150000)
    agent.compact_async = False
    agent.respond("c1", "hi")
    assert len(driver.calls) == 1   # no summary/seed call
    assert store.get("c1") == "s1"


def test_no_compaction_when_disabled(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    driver = FakeDriver([
        ClaudeResult(text="a", session_id="s1", is_error=False),
        ClaudeResult(text="b", session_id="s1", is_error=False),
        ClaudeResult(text="c", session_id="s1", is_error=False),
    ])
    agent = Agent(driver, store, compact_every=0)  # disabled
    agent.compact_async = False
    for _ in range(3):
        agent.respond("c1", "x")
    assert len(driver.calls) == 3   # never an extra summary/seed call
    assert store.get("c1") == "s1"


def test_routing_picks_light_model_for_trivial_turn(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    driver = FakeDriver([ClaudeResult(text="hi", session_id="s1", is_error=False)], model="claude-opus-4-8")
    agent = Agent(driver, store, light_model="claude-haiku-4-5")
    agent.respond("c1", "thanks!")
    assert driver.model_calls[0] == "claude-haiku-4-5"  # trivial -> light


def test_routing_keeps_heavy_model_for_real_turn(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    driver = FakeDriver([ClaudeResult(text="hi", session_id="s1", is_error=False)], model="claude-opus-4-8")
    agent = Agent(driver, store, light_model="claude-haiku-4-5")
    agent.respond("c1", "please debug this stack trace and explain the root cause for me")
    assert driver.model_calls[0] is None  # heavy -> no override, driver default used


def test_no_routing_when_light_model_unset(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    driver = FakeDriver([ClaudeResult(text="hi", session_id="s1", is_error=False)], model="claude-opus-4-8")
    agent = Agent(driver, store)  # no light model
    agent.respond("c1", "thanks!")
    assert driver.model_calls[0] is None  # always the driver default


def test_from_config_builds_agent(tmp_path):
    from iris.config import Config
    cfg = Config(session_store_path=str(tmp_path / "s.json"), model="claude-sonnet-4-6")
    agent = Agent.from_config(cfg)
    assert agent.driver.model == "claude-sonnet-4-6"
    assert agent.driver.append_system_prompt_file == cfg.persona_file
    assert isinstance(agent.store, SessionStore)
