"""Tests for per-context tool gating (iris/gating.py)."""

from __future__ import annotations

from iris.config import Config
from iris.driver import DANGEROUS_BUILTINS
from iris.gating import SELF_STARTING_TOOLS, gate_self_starting


def test_self_starting_tools_are_the_work_creators():
    # The tools that create NEW clock/self-triggered work.
    assert "mcp__jobs__schedule_job" in SELF_STARTING_TOOLS
    assert "mcp__jobs__run_in_background" in SELF_STARTING_TOOLS
    assert "mcp__jobs__start_job" in SELF_STARTING_TOOLS
    assert "mcp__goals__set_goal" in SELF_STARTING_TOOLS


def test_gate_removes_self_starting_from_allowed_and_denies_them():
    cfg = Config(
        allowed_tools=["mcp__jobs__schedule_job", "mcp__memory__recall",
                       "mcp__jobs__run_in_background", "mcp__goals__set_goal",
                       "mcp__jobs__start_job"],
        restrict_builtin_tools=True,
        disallowed_tools=[],
    )
    gated = gate_self_starting(cfg)
    # the work-creators are gone from the allowlist...
    assert gated.allowed_tools == ["mcp__memory__recall"]
    # ...and explicitly denied (deny outranks allow), belt-and-suspenders
    for tool in SELF_STARTING_TOOLS:
        assert tool in gated.disallowed_tools


def test_gate_preserves_the_dangerous_builtin_denylist():
    # restrict_builtin_tools=True with no explicit denylist -> the driver would
    # deny DANGEROUS_BUILTINS; the gate must keep that and ADD the self-starting tools.
    cfg = Config(allowed_tools=["mcp__memory__recall"], restrict_builtin_tools=True,
                 disallowed_tools=[])
    gated = gate_self_starting(cfg)
    for tool in DANGEROUS_BUILTINS:
        assert tool in gated.disallowed_tools
    for tool in SELF_STARTING_TOOLS:
        assert tool in gated.disallowed_tools


def test_gate_keeps_an_explicit_denylist_and_extends_it():
    cfg = Config(allowed_tools=["mcp__memory__recall"], disallowed_tools=["CustomDenied"])
    gated = gate_self_starting(cfg)
    assert "CustomDenied" in gated.disallowed_tools
    for tool in SELF_STARTING_TOOLS:
        assert tool in gated.disallowed_tools


def test_gate_does_not_mutate_the_original_config():
    cfg = Config(allowed_tools=["mcp__jobs__schedule_job", "mcp__memory__recall"])
    gate_self_starting(cfg)
    assert "mcp__jobs__schedule_job" in cfg.allowed_tools  # original untouched
