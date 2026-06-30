"""Tests for the transport-agnostic agent core."""

from __future__ import annotations

from fakes import FakeDriver, FakeStreamDriver

from iris.agent import Agent
from iris.driver import ClaudeResult
from iris.sessions import SessionStore


def test_respond_persists_new_session(tmp_path):
    store = SessionStore(tmp_path / "s.json")
    driver = FakeDriver([ClaudeResult(text="hi", session_id="sess-1", is_error=False)])
    agent = Agent(driver, store)
    result = agent.respond("c1", "hello")
    assert result.text == "hi"
    assert store.get("c1") == "sess-1"


def test_respond_prefetches_relevant_memory_into_the_prompt(tmp_path):
    import json
    mem = tmp_path / "mem.json"
    mem.write_text(json.dumps([
        {"id": 1, "text": "the staging url is staging.example.com",
         "pinned": False, "created_at": "2026-06-01T00:00:00Z"}]), "utf-8")
    store = SessionStore(tmp_path / "s.json")
    driver = FakeDriver([ClaudeResult(text="ok", session_id="s1", is_error=False)])
    agent = Agent(driver, store, memory_file=str(mem), memory_prefetch_bytes=500)
    agent.respond("c1", "what is the staging url again")
    assert "staging.example.com" in driver.calls[0][0]  # the relevant note was prefetched


def test_respond_skips_prefetch_when_disabled(tmp_path):
    import json
    mem = tmp_path / "mem.json"
    mem.write_text(json.dumps([
        {"id": 1, "text": "the staging url is staging.example.com",
         "pinned": False, "created_at": "2026-06-01T00:00:00Z"}]), "utf-8")
    store = SessionStore(tmp_path / "s.json")
    driver = FakeDriver([ClaudeResult(text="ok", session_id="s1", is_error=False)])
    agent = Agent(driver, store, memory_file=str(mem), memory_prefetch_bytes=0)  # off
    agent.respond("c1", "what is the staging url again")
    assert "staging.example.com" not in driver.calls[0][0]


def test_chat_isolate_cwd_runs_the_brain_in_a_scratch_dir(tmp_path):
    # Security: with IRIS_CHAT_ISOLATE_CWD on, the brain's claude child runs in an
    # isolated scratch dir so a `Read ./.env` cannot reach the agent dir's secrets.
    from iris.config import Config
    off = Agent.from_config(Config(chat_isolate_cwd=False))
    assert off.driver.cwd is None
    on = Agent.from_config(Config(chat_isolate_cwd=True))
    assert on.driver.cwd is not None and "iris-brain-" in on.driver.cwd


def test_clock_gated_agent_uses_a_separate_session_store(tmp_path):
    # Regression: a clock tick (proactive/goal) must not share the bot's session
    # file, or its whole-dict flush clobbers sessions the bot wrote in between.
    from iris.config import Config
    cfg = Config(session_store_path=str(tmp_path / "main.json"),
                 clock_session_store=str(tmp_path / "clock.json"))
    bot = Agent.from_config(cfg, clock_gated=False)
    tick = Agent.from_config(cfg, clock_gated=True)
    assert str(bot.store.path).endswith("main.json")
    assert str(tick.store.path).endswith("clock.json")


def test_reset_clears_the_recent_turns_buffer(tmp_path):
    # Regression: !new must drop the buffer too, or the next compaction summarizes
    # the turns the owner just forgot.
    rt = str(tmp_path / "rt.json")
    store = SessionStore(tmp_path / "s.json")
    agent = Agent(FakeDriver([]), store, recent_turns_file=rt)
    agent._record_turn("c1", "secret thing", "noted")
    agent.reset("c1")
    assert agent._recent_transcript("c1") == ""           # in-memory cleared
    fresh = Agent(FakeDriver([]), store, recent_turns_file=rt)
    assert fresh._recent_transcript("c1") == ""           # and persisted clear


def test_recent_turns_buffer_survives_a_restart(tmp_path):
    # The compaction seed used to live only in RAM, so a restart-then-overflow
    # dropped history. Persisting it means a fresh process still has something to
    # summarize instead of skipping compaction entirely.
    rt = str(tmp_path / "rt.json")
    store = SessionStore(tmp_path / "s.json")
    agent = Agent(FakeDriver([]), store, recent_turns_file=rt)
    agent._record_turn("discord:1", "what's the plan", "ship it")
    agent._record_turn("discord:1", "and then", "test it")
    # a brand-new Agent over the same file = a process restart
    agent2 = Agent(FakeDriver([]), store, recent_turns_file=rt)
    transcript = agent2._recent_transcript("discord:1")
    assert "what's the plan" in transcript and "ship it" in transcript and "test it" in transcript


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
        ClaudeResult(text="SUMMARY OF CHAT", session_id=None, is_error=False),  # summary on a fresh session
        ClaudeResult(text="ok", session_id="s2", is_error=False),              # fresh seeded session
    ])
    agent = Agent(driver, store, compact_every=2)
    agent.compact_async = False  # run compaction inline for a deterministic test

    agent.respond("c1", "first")
    assert store.turns("c1") == 1  # no compaction yet
    agent.respond("c1", "second")  # second turn crosses the threshold

    prompts = [p for p, _ in driver.calls]
    # The summary is built from the recent-turns buffer on a FRESH session, never
    # by resuming the live one (so it needn't hold the conversation lock).
    assert any("Summarize the conversation" in p for p in prompts)
    summary_call = next(c for c in driver.calls if "Summarize the conversation" in c[0])
    assert summary_call[1] is None  # a fresh session, not "s1"
    assert "first" in summary_call[0] and "reply 1" in summary_call[0]
    assert "second" in summary_call[0] and "reply 2" in summary_call[0]
    assert any("SUMMARY OF CHAT" in p for p in prompts)  # the summary seeded the new one
    assert store.get("c1") == "s2"  # now on the fresh session
    assert store.turns("c1") == 1   # counter reset for the new session


def test_compaction_summary_runs_off_the_conversation_lock(tmp_path):
    # D4: the summary must not hold the conversation lock, or a compaction-triggered
    # turn blocks every incoming message on that conversation for up to turn_timeout.
    # Proven deterministically: during the summary and seed model calls the
    # conversation lock is free (a non-blocking acquire from this thread succeeds;
    # the lock is non-reentrant, so it would fail if compaction held it).
    store = SessionStore(tmp_path / "s.json")
    free = {}

    class ProbeDriver:
        def __init__(self):
            self.calls = []

        def run(self, prompt, session_id=None, model=None, conversation_id=None):
            self.calls.append((prompt, session_id))
            if "open threads" in prompt:  # the summary call
                got = agent._lock_for("c1").acquire(blocking=False)
                free["summary"] = got
                if got:
                    agent._lock_for("c1").release()
                return ClaudeResult(text="SUMMARY", session_id=None, is_error=False)
            if "continues an earlier one" in prompt:  # the seed call
                got = agent._lock_for("c1").acquire(blocking=False)
                free["seed"] = got
                if got:
                    agent._lock_for("c1").release()
                return ClaudeResult(text="ok", session_id="s2", is_error=False)
            return ClaudeResult(text="reply", session_id="s1", is_error=False)

    driver = ProbeDriver()
    agent = Agent(driver, store, compact_every=1)
    agent.compact_async = False
    agent.respond("c1", "hello there")  # compact_every=1 triggers compaction this turn
    assert free.get("summary") is True  # the fix: summary runs with the lock free
    assert free.get("seed") is True
    assert store.get("c1") == "s2"


def test_compaction_skips_when_the_recent_turns_buffer_is_empty(tmp_path):
    # Off-lock compaction summarizes the recent-turns buffer; with nothing buffered
    # (e.g. right after a restart) it must skip rather than resume the live session.
    store = SessionStore(tmp_path / "s.json")
    store.set("c1", "s1")
    driver = FakeDriver([])  # no model call should happen
    agent = Agent(driver, store)
    assert agent.compact("c1") is False
    assert driver.calls == []
    assert store.get("c1") == "s1"  # session untouched


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
    assert "not instructions" in prompt.lower()  # folded notes are fenced as data
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


async def test_live_turn_records_a_turn_for_compaction(tmp_path):
    # The live path must feed the recent-turns buffer too, or its conversations
    # would never have material for the off-lock compaction summary.
    store = SessionStore(tmp_path / "s.json")
    sd = FakeStreamDriver([ClaudeResult(text="the reply", session_id="s1", is_error=False)])
    agent = Agent(FakeDriver([]), store, stream_driver=sd)
    turn = agent.live_turn("c1", "remember the EUDR deadline")
    await turn.begin()
    await turn.result()
    await turn.aftermath()
    transcript = agent._recent_transcript("c1")
    assert "remember the EUDR deadline" in transcript  # the raw user message
    assert "the reply" in transcript


async def test_live_turn_restores_inbox_on_error(tmp_path):
    from iris.inbox import Inbox

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


def test_from_config_clock_gated_strips_self_starting_tools(tmp_path):
    from iris.config import Config

    cfg = Config(session_store_path=str(tmp_path / "s.json"),
                 allowed_tools=["mcp__jobs__schedule_job", "mcp__memory__recall",
                                "mcp__goals__set_goal", "mcp__jobs__run_in_background"])
    gated = Agent.from_config(cfg, clock_gated=True)
    assert "mcp__memory__recall" in gated.driver.allowed_tools
    for tool in ("mcp__jobs__schedule_job", "mcp__goals__set_goal", "mcp__jobs__run_in_background"):
        assert tool not in (gated.driver.allowed_tools or [])
        assert tool in gated.driver.disallowed_tools
    # chat (ungated) keeps the full control plane
    chat = Agent.from_config(cfg)
    assert "mcp__jobs__schedule_job" in chat.driver.allowed_tools


def test_pinned_digest_is_scoped_to_the_current_conversation(tmp_path):
    import json as _json

    from iris.config import Config

    mem = tmp_path / "mem.json"
    mem.write_text(_json.dumps([
        {"id": 1, "text": "owner prefers metric", "pinned": True},                     # global
        {"id": 2, "text": "the repost plan for the channel", "pinned": True,
         "conversation_id": "111"},                                                    # thread 111 only
    ]), encoding="utf-8")
    cfg = Config(session_store_path=str(tmp_path / "s.json"), memory_file=str(mem))
    agent = Agent.from_config(cfg)

    # Responding in a DIFFERENT thread: the global note loads, the 111 note does not.
    in_222 = agent.driver.system_prompt_extra("discord:222")
    assert "owner prefers metric" in in_222
    assert "repost plan" not in in_222
    # In its own thread, the scoped note loads.
    in_111 = agent.driver.system_prompt_extra("discord:111")
    assert "repost plan" in in_111


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


def test_from_config_wires_approvals_when_enabled(tmp_path):
    from iris.config import Config

    on = Agent.from_config(Config(session_store_path=str(tmp_path / "s.json"), approvals_enabled=True))
    assert on.driver.permission_prompt_tool == "mcp__approvals__check"
    off = Agent.from_config(Config(session_store_path=str(tmp_path / "s2.json")))
    assert off.driver.permission_prompt_tool is None
