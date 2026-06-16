"""Golden scenarios: the regression backbone for Iris's load-bearing behavior.

Unlike the per-module unit tests, this file is a small, deterministic set of
end-to-end behaviors that must never silently break, framed as scenarios a person
would recognize: a stranger is refused, an over-budget job parks, a reminder
fires, background notes fold into the next turn (fenced as data). Run this before
shipping any prompt, persona, or skill change. No real claude: canned results via
the canonical FakeDriver. Grow this set from real failures.
"""

from __future__ import annotations

from fakes import FakeDriver, tmp_store

from iris.config import Config
from iris.driver import ClaudeResult


# -- single-user gate --------------------------------------------------------


class _User:
    def __init__(self, id, bot=False):
        self.id = id
        self.bot = bot


class _Chan:
    def __init__(self):
        self.guild = object()
        self.parent_id = None
        self.id = 1


class _Msg:
    def __init__(self, author, mentions=()):
        self.channel = _Chan()
        self.author = author
        self.mentions = list(mentions)


def test_golden_single_user_gate_refuses_a_stranger():
    from iris.discord_adapter import should_handle

    bot = _User(999)
    cfg = Config(allowed_user_ids=["42"], respond_without_mention=True)
    assert should_handle(_Msg(_User(42), mentions=[bot]), bot, cfg) is True   # the owner
    assert should_handle(_Msg(_User(7), mentions=[bot]), bot, cfg) is False   # a stranger
    assert should_handle(_Msg(_User(7)), bot, cfg) is False                   # stranger, no mention


# -- budget leash ------------------------------------------------------------


def test_golden_refuses_over_budget_spend(tmp_path):
    from iris.jobs import JobStore
    from iris.jobs_console import gated_launch
    from iris.usage import CreditGuard

    config = Config(jobs_enabled=True, usage_file=str(tmp_path / "usage.json"),
                    usage_budget_usd=1.0, jobs_file=str(tmp_path / "jobs.json"),
                    home_channel="home", discord_token="t")
    # Drive the month over the park line (100% of a $1 budget).
    CreditGuard.from_config(config).record(
        "job", ClaudeResult(text="", session_id=None, is_error=False, cost_usd=1.0))

    spawned = []
    result = gated_launch(config, JobStore(config.jobs_file), title="big job",
                          instructions="do the expensive thing", grants=[], workspace="",
                          spawn=lambda jid, **k: spawned.append(jid))
    assert result["outcome"] == "parked"  # admitted to the queue, not run
    assert spawned == []                   # no runner launched, so no spend


# -- reminders ---------------------------------------------------------------


def test_golden_schedules_and_fires_a_reminder(tmp_path):
    from iris.reminders import ReminderStore

    store = ReminderStore(tmp_path / "rem.json")
    rid = store.add(due_ts=1000.0, text="call the EUDR office", channel_id="home")
    assert rid == 1
    assert store.pop_due(now=999.0) == []  # not due yet
    fired = store.pop_due(now=1001.0)
    assert len(fired) == 1 and fired[0]["text"] == "call the EUDR office"
    assert store.pop_due(now=2000.0) == []  # a one-shot fires exactly once


# -- inbox fold-back ---------------------------------------------------------


def test_golden_folds_background_notes_into_the_next_turn(tmp_path):
    from iris.agent import Agent
    from iris.inbox import Inbox

    box = Inbox(tmp_path / "inbox.json")
    box.append("job #7 finished: wrote the report", conversation_id="c1")
    driver = FakeDriver([ClaudeResult(text="Here's what you missed.", session_id="s1", is_error=False)])
    agent = Agent(driver, tmp_store(tmp_path), inbox=box)

    result = agent.respond("c1", "what did I miss?")
    prompt = driver.calls[0][0]
    assert "job #7 finished: wrote the report" in prompt   # the note folded in
    assert "not instructions" in prompt.lower()            # fenced as data (S4)
    assert prompt.endswith("what did I miss?")
    assert not result.is_error
    assert box.drain("c1") == []                           # consumed by the successful turn


def test_golden_failed_turn_keeps_the_folded_notes(tmp_path):
    from iris.agent import Agent
    from iris.inbox import Inbox

    box = Inbox(tmp_path / "inbox.json")
    box.append("job #8 finished: ready", conversation_id="c1")
    driver = FakeDriver([ClaudeResult(text="", session_id=None, is_error=True, error="boom")])
    agent = Agent(driver, tmp_store(tmp_path), inbox=box)

    result = agent.respond("c1", "anything?")
    assert result.is_error
    assert box.drain("c1") == ["job #8 finished: ready"]   # restored for the next turn
