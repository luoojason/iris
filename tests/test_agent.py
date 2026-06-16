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

    def run(self, prompt, session_id=None, model=None, conversation_id=None):
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

        def run(self, prompt, session_id=None, model=None, conversation_id=None):
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


def test_respond_forces_model_override(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    driver = FakeDriver([ClaudeResult(text="hi", session_id="s1", is_error=False)], model="claude-opus-4-8")
    agent = Agent(driver, store, light_model="claude-haiku-4-5")
    agent.respond("c1", "thanks!", model="claude-opus-4-8")  # trivial, but forced
    assert driver.model_calls[0] == "claude-opus-4-8"  # router bypassed


def test_async_compaction_runs_in_background(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    driver = FakeDriver([
        ClaudeResult(text="reply", session_id="s1", is_error=False, context_tokens=160000),
        ClaudeResult(text="SUMMARY", session_id="s1", is_error=False),
        ClaudeResult(text="ok", session_id="s2", is_error=False),
    ])
    agent = Agent(driver, store, compact_at_tokens=150000)  # compact_async defaults True
    agent.respond("c1", "fetch a huge page")
    thread = agent._last_compaction
    assert thread is not None
    thread.join(timeout=5)
    assert store.get("c1") == "s2"  # the background thread compacted onto a fresh session


def test_failed_compaction_keeps_session_and_backs_off(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    driver = FakeDriver([
        ClaudeResult(text="reply", session_id="s1", is_error=False, context_tokens=160000),
        ClaudeResult(text="", session_id="s1", is_error=True, error="boom"),  # summary fails
        ClaudeResult(text="reply2", session_id="s1", is_error=False, context_tokens=160000),
    ])
    agent = Agent(driver, store, compact_at_tokens=150000)
    agent.compact_async = False
    agent.respond("c1", "big one")
    assert store.get("c1") == "s1"            # session kept after a failed summary
    assert "c1" in agent._compact_cooldown_until
    agent.respond("c1", "another big one")    # still inside the cooldown window
    assert len(driver.calls) == 3             # no second doomed summary attempt
    assert store.get("c1") == "s1"


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


# -- fold-back inbox ---------------------------------------------------------


def test_respond_folds_inbox_entries_into_the_prompt(tmp_path):
    from iris.inbox import Inbox

    store = SessionStore(tmp_path / "s.json")
    box = Inbox(tmp_path / "inbox.json")
    box.append("job #1 (audit) finished: all clean", conversation_id="c1")
    driver = FakeDriver([ClaudeResult(text="hi", session_id="s1", is_error=False)])
    agent = Agent(driver, store, inbox=box)
    agent.respond("c1", "hello")
    prompt = driver.calls[0][0]
    assert prompt.startswith("[while you were away]")
    assert "job #1 (audit) finished: all clean" in prompt
    assert prompt.endswith("hello")
    assert box.drain("c1") == []  # consumed by the successful turn


def test_failed_turn_restores_inbox_entries(tmp_path):
    from iris.inbox import Inbox

    store = SessionStore(tmp_path / "s.json")
    box = Inbox(tmp_path / "inbox.json")
    box.append("job #2 finished: report text", conversation_id="c1")
    driver = FakeDriver([ClaudeResult(text="", session_id=None, is_error=True, error="boom")])
    agent = Agent(driver, store, inbox=box)
    result = agent.respond("c1", "hello")
    assert result.is_error
    # a flaky turn must not eat the report; it comes back next turn
    assert box.drain("c1") == ["job #2 finished: report text"]


def test_empty_inbox_leaves_the_prompt_alone(tmp_path):
    from iris.inbox import Inbox

    store = SessionStore(tmp_path / "s.json")
    box = Inbox(tmp_path / "inbox.json")
    driver = FakeDriver([ClaudeResult(text="hi", session_id="s1", is_error=False)])
    agent = Agent(driver, store, inbox=box)
    agent.respond("c1", "hello")
    assert driver.calls[0][0] == "hello"


async def test_live_turn_folds_inbox_and_consumes_on_success(tmp_path):
    from iris.inbox import Inbox

    class FakeStreamTurn:
        def __init__(self, result):
            self._result = result
            self.open = False
            self.strays = []

        def wait_primary(self, timeout=None):
            return self._result

        def wait_finished(self, timeout=None):
            return True

    class FakeStreamDriver:
        def __init__(self, results):
            self.results = list(results)
            self.prompts = []

        def start(self, prompt, session_id=None, model=None):
            self.prompts.append(prompt)
            return FakeStreamTurn(self.results.pop(0))

    store = SessionStore(tmp_path / "s.json")
    box = Inbox(tmp_path / "inbox.json")
    box.append("job #3 finished: done", conversation_id="c1")
    sd = FakeStreamDriver([ClaudeResult(text="hi", session_id="s1", is_error=False)])
    agent = Agent(FakeDriver([]), store, stream_driver=sd, inbox=box)
    turn = agent.live_turn("c1", "hello")
    await turn.begin()
    result = await turn.result()
    await turn.aftermath()
    assert not result.is_error
    assert sd.prompts[0].startswith("[while you were away]")
    assert "job #3 finished: done" in sd.prompts[0]
    assert sd.prompts[0].endswith("hello")
    assert box.drain("c1") == []


async def test_live_turn_restores_inbox_on_error(tmp_path):
    from iris.inbox import Inbox

    class FakeStreamTurn:
        def __init__(self, result):
            self._result = result
            self.open = False
            self.strays = []

        def wait_primary(self, timeout=None):
            return self._result

        def wait_finished(self, timeout=None):
            return True

    class FakeStreamDriver:
        def __init__(self, results):
            self.results = list(results)
            self.prompts = []

        def start(self, prompt, session_id=None, model=None):
            self.prompts.append(prompt)
            return FakeStreamTurn(self.results.pop(0))

    store = SessionStore(tmp_path / "s.json")
    box = Inbox(tmp_path / "inbox.json")
    box.append("job #4 finished: report", conversation_id="c1")
    sd = FakeStreamDriver([
        ClaudeResult(text="", session_id=None, is_error=True, error="boom"),
    ])
    agent = Agent(FakeDriver([]), store, stream_driver=sd, inbox=box)
    turn = agent.live_turn("c1", "hello")
    await turn.begin()
    result = await turn.result()
    await turn.aftermath()
    assert result.is_error
    assert box.drain("c1") == ["job #4 finished: report"]


def test_raising_driver_restores_inbox_entries(tmp_path):
    """ClaudeError raises out of respond (adapters catch it); the drained
    notes must be restored exactly as they are for an error result."""
    import pytest

    from iris.driver import ClaudeError
    from iris.inbox import Inbox

    class RaisingDriver:
        model = None

        def run(self, prompt, session_id=None, model=None, conversation_id=None):
            raise ClaudeError("claude binary not found")

    store = SessionStore(tmp_path / "s.json")
    box = Inbox(tmp_path / "inbox.json")
    box.append("job #5 finished: do not lose me", conversation_id="c1")
    agent = Agent(RaisingDriver(), store, inbox=box)
    with pytest.raises(ClaudeError):
        agent.respond("c1", "hello")
    assert box.drain("c1") == ["job #5 finished: do not lose me"]


def test_from_config_wires_standing_orders(tmp_path):
    from iris.config import Config
    cfg = Config(
        session_store_path=str(tmp_path / "s.json"),
        standing_orders_file=str(tmp_path / "orders.md"),
    )
    agent = Agent.from_config(cfg)
    assert agent.driver.standing_orders_file == cfg.standing_orders_file


def test_from_config_wires_the_pinned_memory_digest(tmp_path):
    import json as _json

    from iris.config import Config

    mem = tmp_path / "mem.json"
    mem.write_text(_json.dumps([
        {"id": 1, "text": "owner prefers metric", "pinned": True},
        {"id": 2, "text": "unpinned chatter"},
    ]), encoding="utf-8")
    cfg = Config(session_store_path=str(tmp_path / "s.json"), memory_file=str(mem))
    agent = Agent.from_config(cfg)
    assert agent.driver.system_prompt_extra is not None
    block = agent.driver.system_prompt_extra()
    assert "owner prefers metric" in block
    assert "unpinned chatter" not in block


def test_memory_digest_supplier_tolerates_a_broken_store(tmp_path):
    from iris.agent import _memory_digest_supplier

    missing = _memory_digest_supplier(str(tmp_path / "absent.json"), 2400)
    assert missing() == ""
    corrupt = tmp_path / "bad.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert _memory_digest_supplier(str(corrupt), 2400)() == ""


def test_jobs_digest_supplier_renders_active_jobs_and_tolerates_breakage(tmp_path):
    import json as _json

    from iris.agent import _jobs_digest_supplier

    jobs = tmp_path / "jobs.json"
    jobs.write_text(_json.dumps([
        {"id": 27, "state": "running", "title": "Publish 5 parked Top-5 Shorts", "finished_ts": None},
    ]), encoding="utf-8")
    out = _jobs_digest_supplier(str(jobs), 600, 3600)()
    assert "#27 [running] Publish 5 parked Top-5 Shorts" in out
    # broken/missing registry -> empty, never raises
    assert _jobs_digest_supplier(str(tmp_path / "absent.json"), 600, 3600)() == ""
    corrupt = tmp_path / "bad.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert _jobs_digest_supplier(str(corrupt), 600, 3600)() == ""


def test_from_config_composes_memory_and_active_jobs_digests(tmp_path):
    import json as _json

    from iris.config import Config

    mem = tmp_path / "mem.json"
    mem.write_text(_json.dumps([{"id": 1, "text": "owner prefers metric", "pinned": True}]), "utf-8")
    jobs = tmp_path / "jobs.json"
    jobs.write_text(_json.dumps([
        {"id": 27, "state": "running", "title": "Publish 5 parked Top-5 Shorts", "finished_ts": None},
    ]), encoding="utf-8")
    cfg = Config(session_store_path=str(tmp_path / "s.json"), memory_file=str(mem),
                 jobs_enabled=True, jobs_file=str(jobs))
    agent = Agent.from_config(cfg)
    block = agent.driver.system_prompt_extra()
    # both tier-0 blocks present in one composed prompt extra — the regression guard:
    # a turn now SEES the in-flight #27 and would not relaunch it.
    assert "owner prefers metric" in block
    assert "#27 [running] Publish 5 parked Top-5 Shorts" in block


def test_memory_digest_supplier_halves_budget_when_hot(tmp_path):
    import json as _json

    from iris.agent import _memory_digest_supplier

    mem = tmp_path / "mem.json"
    mem.write_text(_json.dumps([
        {"id": 1, "text": "f" * 200, "pinned": True},
    ]), encoding="utf-8")

    class HotGuard:
        def level(self):
            return "tighten"

    # the note fits the full budget but not half of it
    cool = _memory_digest_supplier(str(mem), 400)
    hot = _memory_digest_supplier(str(mem), 400, guard=HotGuard())
    assert "fff" in cool()
    assert hot() == ""


def test_respond_skips_the_session_write_after_a_reset(tmp_path):
    # A reset that lands while a turn is in flight (e.g. via !new or !stop) must
    # win: the finishing turn must not resurrect the session the user cleared.
    store = SessionStore(tmp_path / "s.json")
    store.set("c1", "old")

    class ResettingDriver:
        model = None

        def __init__(self, agent_box):
            self.agent_box = agent_box

        def run(self, prompt, session_id=None, model=None, conversation_id=None):
            self.agent_box[0].reset("c1")  # the user reset mid-turn
            return ClaudeResult(text="hi", session_id="brand-new", is_error=False)

    box = []
    agent = Agent(ResettingDriver(box), store)
    box.append(agent)
    agent.respond("c1", "hello")
    assert store.get("c1") is None  # the reset stuck; no resurrection
