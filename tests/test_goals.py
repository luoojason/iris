"""Tests for the goal loop (iris/goals.py).

A goal is a standing objective the clock advances one work-step at a time until
it is done or needs the owner. The store is file-backed; the tick is gated on
the same real-weekly-usage leash as the proactive reviews, steps via a model
turn, and judges progress with an independent cheap-model check. The seams
(step, judge, sender, fetch) are injected so no real model or network runs here.
"""

from __future__ import annotations

from iris.config import Config
from iris.goals import GoalStore, parse_confirmation, parse_verdict, run_goal_tick


def test_parse_confirmation_reads_confirmed_and_unconfirmed():
    assert parse_confirmation("CONFIRMED: the wiki page exists")["confirmed"] is True
    assert parse_confirmation("UNCONFIRMED: nothing was written")["confirmed"] is False
    assert parse_confirmation("UNCONFIRMED: x")["note"] == "x"
    # unreadable reply is conservative: not confirmed
    assert parse_confirmation("I'm not sure")["confirmed"] is False
    assert parse_confirmation("")["confirmed"] is False


# -- parse_verdict -----------------------------------------------------------

def test_parse_verdict_reads_each_status():
    assert parse_verdict("DONE: it is finished")["status"] == "done"
    assert parse_verdict("BLOCKED: need a credential")["status"] == "blocked"
    assert parse_verdict("CONTINUE: more to do")["status"] == "continue"
    assert parse_verdict("DONE: shipped")["summary"] == "shipped"


def test_parse_verdict_tolerates_a_leading_line():
    assert parse_verdict("Here's my call:\nCONTINUE: keep at it")["status"] == "continue"


def test_parse_verdict_requires_a_word_boundary_not_a_prefix():
    # "DONENESS"/"CONTINUED" must not match the verdict tokens as bare prefixes.
    assert parse_verdict("DONENESS is unclear, keep at it")["status"] == "blocked"
    assert parse_verdict("CONTINUE: keep going")["status"] == "continue"
    assert parse_verdict("DONE: it is finished")["status"] == "done"


def test_parse_verdict_unreadable_reply_fails_open_to_blocked():
    # No recognizable verdict -> the judge did not rule -> ask the owner.
    assert parse_verdict("I'm honestly not sure about this")["status"] == "blocked"
    assert parse_verdict("")["status"] == "blocked"
    # "not done" mid-sentence must not trip a false DONE
    assert parse_verdict("This is not done yet, needs more")["status"] == "blocked"


# -- GoalStore ---------------------------------------------------------------

def test_add_creates_an_active_goal_and_persists(tmp_path):
    store = GoalStore(tmp_path / "g.json")
    goal = store.add("ship the roadmap", conversation_id="discord:chan-9",
                     max_steps=5, now=100.0)
    assert goal["status"] == "active"
    assert goal["text"] == "ship the roadmap"
    assert goal["conversation_id"] == "discord:chan-9"
    assert goal["max_steps"] == 5
    assert goal["steps"] == 0
    assert goal["log"] == []
    assert goal["created_ts"] == 100.0 and goal["updated_ts"] == 100.0
    # survives a reload
    assert GoalStore(tmp_path / "g.json").get(goal["id"]) == goal


def test_ids_increment_and_all_lists_in_order(tmp_path):
    store = GoalStore(tmp_path / "g.json")
    a = store.add("first", now=1.0)
    b = store.add("second", now=2.0)
    assert b["id"] == a["id"] + 1
    assert [g["id"] for g in store.all()] == [a["id"], b["id"]]


def test_update_and_transition(tmp_path):
    store = GoalStore(tmp_path / "g.json")
    g = store.add("do a thing", now=1.0)
    store.update(g["id"], steps=2, log=[{"step": "x"}])
    store.transition(g["id"], "done", now=9.0)
    reloaded = GoalStore(tmp_path / "g.json").get(g["id"])
    assert reloaded["steps"] == 2
    assert reloaded["log"] == [{"step": "x"}]
    assert reloaded["status"] == "done"
    assert reloaded["updated_ts"] == 9.0


def test_transition_if_active_refuses_a_non_active_goal(tmp_path):
    # The step-budget block (and any terminal flip) must never overwrite a cancel
    # the owner landed first; transition_if_active no-ops on a non-active goal.
    store = GoalStore(tmp_path / "g.json")
    g = store.add("do a thing", now=1.0)
    store.transition(g["id"], "cancelled", now=2.0)
    assert store.transition_if_active(g["id"], "blocked", now=3.0) is None
    assert store.get(g["id"])["status"] == "cancelled"
    # an active goal still transitions normally
    h = store.add("another", now=4.0)
    assert store.transition_if_active(h["id"], "blocked", now=5.0) is not None
    assert store.get(h["id"])["status"] == "blocked"


def test_active_filters_terminal_goals(tmp_path):
    store = GoalStore(tmp_path / "g.json")
    a = store.add("active one", now=1.0)
    b = store.add("done one", now=2.0)
    store.transition(b["id"], "done", now=3.0)
    assert [g["id"] for g in store.active()] == [a["id"]]


# -- run_goal_tick -----------------------------------------------------------

def _cfg(tmp_path, **kw):
    # goals_verify_done defaults OFF here so the existing tick tests don't reach
    # the real verifier; the verify-path tests below turn it on and inject a seam.
    base = dict(goals_enabled=True, home_channel="home-1", discord_token="tok",
                goals_file=str(tmp_path / "goals.json"),
                goals_verify_done=False,
                proactive_usage_cache=str(tmp_path / "weekly.json"),
                usage_file=str(tmp_path / "usage.json"))
    base.update(kw)
    return Config(**base)


def _seed(tmp_path, **kw):
    store = GoalStore(tmp_path / "goals.json")
    goal = store.add(kw.pop("text", "reach the goal"), now=kw.pop("now", 1.0), **kw)
    return store, goal


def test_tick_disabled_does_nothing(tmp_path):
    _seed(tmp_path)
    steps = []
    cfg = _cfg(tmp_path, goals_enabled=False)
    status = run_goal_tick(cfg, now=10.0, fetch=lambda: 5.0,
                           step=lambda g: steps.append(g) or "did", judge=None,
                           sender=lambda c, t, k: None)
    assert status == "disabled"
    assert steps == []  # never stepped a goal


def test_tick_skips_over_the_weekly_threshold(tmp_path):
    _seed(tmp_path)
    steps, sent = [], []
    status = run_goal_tick(_cfg(tmp_path), now=10.0, fetch=lambda: 90.0,
                           step=lambda g: steps.append(g) or "did",
                           judge=lambda g, t: {"status": "continue"},
                           sender=lambda c, t, k: sent.append(t))
    assert status.startswith("skipped")
    assert steps == [] and sent == []  # no model call, no spend


def test_tick_idle_when_no_active_goals(tmp_path):
    GoalStore(tmp_path / "goals.json")  # empty store
    steps = []
    status = run_goal_tick(_cfg(tmp_path), now=10.0, fetch=lambda: 5.0,
                           step=lambda g: steps.append(g) or "did",
                           judge=lambda g, t: {"status": "continue"},
                           sender=lambda c, t, k: None)
    assert status == "idle"
    assert steps == []


def test_tick_advances_silently_on_continue(tmp_path):
    store, goal = _seed(tmp_path, max_steps=5)
    sent = []
    status = run_goal_tick(_cfg(tmp_path), now=20.0, fetch=lambda: 5.0,
                           step=lambda g: "made some progress",
                           judge=lambda g, t: {"status": "continue", "summary": "keep going"},
                           sender=lambda c, t, k: sent.append(t))
    assert status == "advanced"
    assert sent == []  # progress is silent; no Discord noise
    after = store.get(goal["id"])
    assert after["status"] == "active"
    assert after["steps"] == 1
    assert after["updated_ts"] == 20.0
    assert after["log"][0]["summary"] == "keep going"


def test_tick_reports_done_to_the_origin_thread(tmp_path):
    store, goal = _seed(tmp_path, text="finish the report",
                        conversation_id="discord:chan-9", max_steps=5)
    sent = []
    status = run_goal_tick(_cfg(tmp_path), now=30.0, fetch=lambda: 5.0,
                           step=lambda g: "wrote the last section",
                           judge=lambda g, t: {"status": "done", "summary": "report complete"},
                           sender=lambda c, t, k: sent.append((c, t)))
    assert status == "done"
    assert store.get(goal["id"])["status"] == "done"
    assert sent and sent[0][0] == "chan-9"  # routed to the originating thread
    assert "finish the report" in sent[0][1] or "report complete" in sent[0][1]


def test_tick_blocks_and_asks_the_owner(tmp_path):
    store, goal = _seed(tmp_path, max_steps=5)  # no conversation_id -> home
    sent = []
    status = run_goal_tick(_cfg(tmp_path), now=40.0, fetch=lambda: 5.0,
                           step=lambda g: "hit a fork",
                           judge=lambda g, t: {"status": "blocked", "summary": "need a decision"},
                           sender=lambda c, t, k: sent.append((c, t)))
    assert status == "blocked"
    assert store.get(goal["id"])["status"] == "blocked"
    assert sent and sent[0][0] == "home-1"
    assert "need a decision" in sent[0][1]


def test_tick_judge_error_fails_open_to_asking(tmp_path):
    store, goal = _seed(tmp_path, max_steps=5)
    sent = []

    def boom(goal, text):
        raise RuntimeError("judge model unreachable")

    status = run_goal_tick(_cfg(tmp_path), now=50.0, fetch=lambda: 5.0,
                           step=lambda g: "did work but can't verify",
                           judge=boom, sender=lambda c, t, k: sent.append((c, t)))
    # fail-open: it asks the owner rather than silently looping or claiming done
    assert status == "blocked"
    assert store.get(goal["id"])["status"] == "blocked"
    assert sent  # the owner was pinged


def test_tick_verifies_a_done_claim_and_accepts_when_confirmed(tmp_path):
    store, goal = _seed(tmp_path, text="document the runbook", max_steps=5)
    sent, verified = [], []
    status = run_goal_tick(_cfg(tmp_path, goals_verify_done=True), now=30.0, fetch=lambda: 5.0,
                           step=lambda g: "wrote the runbook page",
                           judge=lambda g, t: {"status": "done", "summary": "page written"},
                           verify=lambda g, t: verified.append((g["id"], t)) or {"confirmed": True, "note": "wiki page present"},
                           sender=lambda c, t, k: sent.append(t))
    assert status == "done"
    assert verified  # the done claim was independently checked
    assert store.get(goal["id"])["status"] == "done"
    assert sent  # owner told it's done


def test_tick_blocks_a_done_claim_verification_cannot_confirm(tmp_path):
    store, goal = _seed(tmp_path, max_steps=5)
    sent = []
    status = run_goal_tick(_cfg(tmp_path, goals_verify_done=True), now=30.0, fetch=lambda: 5.0,
                           step=lambda g: "claims it's done",
                           judge=lambda g, t: {"status": "done", "summary": "all set"},
                           verify=lambda g, t: {"confirmed": False, "note": "no such wiki page exists"},
                           sender=lambda c, t, k: sent.append(t))
    assert status == "blocked"  # not silently completed
    assert store.get(goal["id"])["status"] == "blocked"
    assert sent and "no such wiki page exists" in sent[0]


def test_tick_verify_error_blocks_to_ask_the_owner(tmp_path):
    store, goal = _seed(tmp_path, max_steps=5)
    sent = []

    def boom(g, t):
        raise RuntimeError("verifier unreachable")

    status = run_goal_tick(_cfg(tmp_path, goals_verify_done=True), now=30.0, fetch=lambda: 5.0,
                           step=lambda g: "claims done", judge=lambda g, t: {"status": "done", "summary": "x"},
                           verify=boom, sender=lambda c, t, k: sent.append(t))
    assert status == "blocked"  # fail open: ask rather than accept an unverified done
    assert store.get(goal["id"])["status"] == "blocked"
    assert sent


def test_tick_does_not_verify_a_continue_verdict(tmp_path):
    store, goal = _seed(tmp_path, max_steps=5)
    verified = []
    status = run_goal_tick(_cfg(tmp_path, goals_verify_done=True), now=20.0, fetch=lambda: 5.0,
                           step=lambda g: "made progress",
                           judge=lambda g, t: {"status": "continue", "summary": "more to do"},
                           verify=lambda g, t: verified.append(g) or {"confirmed": True, "note": ""},
                           sender=lambda c, t, k: None)
    assert status == "advanced"
    assert verified == []  # verification only fires on a done claim (bounded cost)


def test_tick_does_not_clobber_a_cancel_during_the_step(tmp_path):
    # The owner cancels the goal while the step/judge is running. The tick must
    # not overwrite that with "done" or ping the owner about a goal they dropped.
    store, goal = _seed(tmp_path, max_steps=5)
    sent = []

    def judge_then_owner_cancels(g, t):
        store.transition(g["id"], "cancelled", now=99.0)
        return {"status": "done", "summary": "claims done"}

    status = run_goal_tick(_cfg(tmp_path), now=40.0, fetch=lambda: 5.0,
                           step=lambda g: "did work",
                           judge=judge_then_owner_cancels,
                           sender=lambda c, t, k: sent.append(t))
    assert status == "cancelled"
    assert store.get(goal["id"])["status"] == "cancelled"  # cancel preserved
    assert sent == []  # no "done" ping for a cancelled goal


def test_tick_budget_exhausted_blocks_without_stepping(tmp_path):
    store, goal = _seed(tmp_path, max_steps=2)
    store.update(goal["id"], steps=2)  # already at the cap
    steps, sent = [], []
    status = run_goal_tick(_cfg(tmp_path), now=60.0, fetch=lambda: 5.0,
                           step=lambda g: steps.append(g) or "should not run",
                           judge=lambda g, t: {"status": "continue"},
                           sender=lambda c, t, k: sent.append((c, t)))
    assert status == "budget"
    assert steps == []  # budget check happens before any model call
    assert store.get(goal["id"])["status"] == "blocked"
    assert sent  # told the owner the budget ran out


def test_tick_advances_the_least_recently_worked_goal(tmp_path):
    store = GoalStore(tmp_path / "goals.json")
    older = store.add("older, waited longest", now=1.0)
    newer = store.add("newer", now=2.0)
    store.update(newer["id"], updated_ts=0.5)  # newer was actually worked more recently? make older the stalest
    worked = []
    run_goal_tick(_cfg(tmp_path), now=70.0, fetch=lambda: 5.0,
                  step=lambda g: worked.append(g["id"]) or "did",
                  judge=lambda g, t: {"status": "continue"},
                  sender=lambda c, t, k: None)
    # the goal with the smallest updated_ts (newer, set to 0.5) is worked first
    assert worked == [newer["id"]]
