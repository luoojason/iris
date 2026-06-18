"""Tests for the approval policy + rendezvous store (iris/approvals.py)."""

from __future__ import annotations

from iris.approvals import ApprovalStore, needs_approval
from iris.config import Config


# -- risk policy (gates on arguments, not just tool name) --------------------


def test_publish_always_needs_approval():
    ok, reason = needs_approval("mcp__publish__publish_video",
                                {"mp4_path": "/x.mp4", "caption": "c"}, Config())
    assert ok is True and reason


def test_powerful_job_launch_needs_approval():
    for tool in ("mcp__jobs__start_job", "mcp__jobs__run_in_background", "mcp__jobs__schedule_job"):
        ok, _ = needs_approval(tool, {"grants": "shell,files"}, Config())
        assert ok is True, tool
    ok, _ = needs_approval("mcp__jobs__start_job", {"heavy": True}, Config())
    assert ok is True


def test_ordinary_job_launch_is_auto_allowed():
    ok, _ = needs_approval("mcp__jobs__start_job", {"grants": "subagents"}, Config())
    assert ok is False


def test_low_risk_tools_auto_allow():
    assert needs_approval("mcp__memory__recall", {"query": "x"}, Config())[0] is False
    assert needs_approval("Read", {"file_path": "/x"}, Config())[0] is False


# -- rendezvous store --------------------------------------------------------


def test_store_create_then_pending_then_record(tmp_path):
    s = ApprovalStore(tmp_path / "approvals.json")
    s.create("r1", "publish a video", now=1000.0)
    assert s.get("r1")["decision"] is None  # awaiting a tap
    assert s.record("r1", "allow", by="owner", now=1001.0) is True
    entry = s.get("r1")
    assert entry["decision"] == "allow" and entry["by"] == "owner"


def test_store_first_decision_wins(tmp_path):
    s = ApprovalStore(tmp_path / "approvals.json")
    s.create("r1", "x", now=1000.0)
    assert s.record("r1", "allow", by="owner", now=1001.0) is True
    assert s.record("r1", "deny", by="owner", now=1002.0) is False  # already decided
    assert s.get("r1")["decision"] == "allow"


def test_store_record_unknown_request_is_false(tmp_path):
    s = ApprovalStore(tmp_path / "approvals.json")
    assert s.record("missing", "allow", by="owner", now=1.0) is False


def test_store_get_missing_is_none(tmp_path):
    assert ApprovalStore(tmp_path / "approvals.json").get("nope") is None


# -- decide(): allow-fast-path / approve / deny / timeout / fail-closed -------

import json as _json

from iris.approvals import decide, format_decision


def _allow(rid, summ):
    return True  # a poster that reaches the owner


def test_decide_auto_allows_safe_calls(tmp_path):
    store = ApprovalStore(tmp_path / "a.json")
    out = decide("mcp__memory__recall", {"query": "x"}, Config(),
                 store=store, post=_allow, now_fn=lambda: 0.0)
    assert _json.loads(out) == {"behavior": "allow"}


def test_decide_allows_when_owner_taps_approve(tmp_path):
    store = ApprovalStore(tmp_path / "a.json")
    clock = [1000.0]

    def sleep_fn(s):
        store.record("R", "allow", by="owner", now=clock[0])  # owner taps during the wait
        clock[0] += s

    out = decide("mcp__publish__publish_video", {"mp4_path": "/x"}, Config(),
                 store=store, post=_allow, now_fn=lambda: clock[0], sleep_fn=sleep_fn,
                 timeout=300, req_id="R")
    assert _json.loads(out)["behavior"] == "allow"


def test_decide_denies_when_owner_taps_deny(tmp_path):
    store = ApprovalStore(tmp_path / "a.json")
    clock = [1000.0]

    def sleep_fn(s):
        store.record("R", "deny", by="owner", now=clock[0])
        clock[0] += s

    out = decide("mcp__publish__publish_video", {"mp4_path": "/x"}, Config(),
                 store=store, post=_allow, now_fn=lambda: clock[0], sleep_fn=sleep_fn,
                 timeout=300, req_id="R")
    assert _json.loads(out)["behavior"] == "deny"


def test_decide_fails_closed_on_timeout(tmp_path):
    store = ApprovalStore(tmp_path / "a.json")
    clock = [0.0]
    out = decide("mcp__publish__publish_video", {"mp4_path": "/x"}, Config(),
                 store=store, post=_allow, now_fn=lambda: clock[0],
                 sleep_fn=lambda s: clock.__setitem__(0, clock[0] + s),
                 timeout=10, poll_secs=4, req_id="R")
    assert _json.loads(out)["behavior"] == "deny"  # never tapped -> denied


def test_decide_fails_closed_when_owner_unreachable(tmp_path):
    store = ApprovalStore(tmp_path / "a.json")
    out = decide("mcp__publish__publish_video", {"mp4_path": "/x"}, Config(),
                 store=store, post=lambda rid, summ: False,  # could not post
                 now_fn=lambda: 0.0, req_id="R")
    assert _json.loads(out)["behavior"] == "deny"
