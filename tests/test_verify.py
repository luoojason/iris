"""Tests for the job verification gate (iris/verify.py)."""

from __future__ import annotations

from iris.config import Config
from iris.verify import parse_verify, verify_result


def test_parse_verify_reads_pass_and_fail():
    assert parse_verify("PASS: the file was written")["ok"] is True
    assert parse_verify("FAIL: nothing was produced")["ok"] is False
    assert parse_verify("PASS: done")["reason"] == "done"


def test_parse_verify_tolerates_a_leading_line():
    assert parse_verify("My review:\nFAIL: it only stubbed the function")["ok"] is False


def test_parse_verify_unreadable_reply_is_unsure_not_a_pass():
    # An unreadable verdict must never read as a silent PASS; it is "unsure" (None)
    # so the report is delivered without a false all-clear.
    assert parse_verify("I can't really tell")["ok"] is None
    assert parse_verify("")["ok"] is None


def test_verify_result_uses_the_injected_judge():
    cfg = Config()
    out = verify_result(cfg, "do X", "did X", judge=lambda i, r: {"ok": True, "reason": "ok"})
    assert out["ok"] is True


def test_verify_result_fails_open_when_the_judge_raises():
    cfg = Config()

    def boom(instructions, report):
        raise RuntimeError("judge model down")

    out = verify_result(cfg, "do X", "did X", judge=boom)
    assert out["ok"] is None  # unavailable, not a pass and not a hard fail
