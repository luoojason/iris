"""Tests for the security/compliance self-audit (iris/audit.py)."""

from __future__ import annotations

import os

from iris.audit import (
    Finding,
    check_chat_sandbox,
    check_publish_dir,
    check_secrets_mode,
    check_single_user,
    check_trace_privacy,
    check_usage_budget,
    render_audit,
    run_audit,
    worst_severity,
)
from iris.config import Config


def _codes(findings):
    return {f.code for f in findings}


def _by_code(findings, code):
    return next((f for f in findings if f.code == code), None)


# -- severity ordering / rendering -------------------------------------------


def test_worst_severity_orders_correctly():
    assert worst_severity([]) == "ok"
    assert worst_severity([Finding("low", "a", "t", "d")]) == "low"
    assert worst_severity([Finding("low", "a", "t", "d"),
                           Finding("critical", "b", "t", "d"),
                           Finding("medium", "c", "t", "d")]) == "critical"


def test_render_groups_by_severity_and_summarizes():
    out = render_audit([Finding("critical", "c1", "Chat can shell", "detail", "fix it"),
                        Finding("info", "i1", "all good", "detail")])
    assert "critical" in out.lower()
    assert "Chat can shell" in out
    assert "1 critical" in out  # summary header counts


# -- chat sandbox (critical) -------------------------------------------------


def test_chat_sandbox_ok_with_default_restrict():
    # Default config restricts the dangerous builtins -> chat cannot shell.
    assert check_chat_sandbox(Config()) == []


def test_chat_secret_reach_flags_a_readable_env_in_cwd(tmp_path):
    from iris.audit import check_chat_secret_reach
    env = tmp_path / ".env"
    env.write_text("IRIS_DISCORD_TOKEN=secret", "utf-8")
    # default config: Read enabled, cwd not isolated -> flagged
    f = _by_code(check_chat_secret_reach(Config(), env_path=env), "chat-secret-reach")
    assert f is not None and f.severity == "high"
    # isolating the brain cwd closes it
    assert check_chat_secret_reach(Config(chat_isolate_cwd=True), env_path=env) == []
    # no .env present -> nothing to reach
    assert check_chat_secret_reach(Config(), env_path=tmp_path / "absent.env") == []


def test_chat_sandbox_critical_when_unrestricted_and_dangerous_allowed():
    cfg = Config(restrict_builtin_tools=False, allowed_tools=["Bash", "Read"])
    f = _by_code(check_chat_sandbox(cfg), "chat-sandbox")
    assert f is not None and f.severity == "critical"


def test_chat_sandbox_critical_when_denylist_misses_a_dangerous_tool():
    # An explicit denylist that forgets Bash leaves chat able to shell.
    cfg = Config(disallowed_tools=["Write", "Edit"], allowed_tools=["Bash"])
    f = _by_code(check_chat_sandbox(cfg), "chat-sandbox")
    assert f is not None and f.severity == "critical"


# -- publish dir (high, S5) --------------------------------------------------


def test_publish_dir_high_when_tool_allowed_but_dir_unset(monkeypatch):
    monkeypatch.delenv("IRIS_PUBLISH_DIR", raising=False)
    cfg = Config(allowed_tools=["mcp__publish__publish_video"])
    f = _by_code(check_publish_dir(cfg), "publish-dir")
    assert f is not None and f.severity == "high"


def test_publish_dir_ok_when_dir_set(monkeypatch, tmp_path):
    monkeypatch.setenv("IRIS_PUBLISH_DIR", str(tmp_path))
    cfg = Config(allowed_tools=["mcp__publish__publish_video"])
    assert check_publish_dir(cfg) == []


def test_publish_dir_ok_when_tool_not_allowed(monkeypatch):
    monkeypatch.delenv("IRIS_PUBLISH_DIR", raising=False)
    assert check_publish_dir(Config(allowed_tools=["WebSearch"])) == []


# -- usage budget (medium) ---------------------------------------------------


def test_usage_budget_medium_when_zero():
    f = _by_code(check_usage_budget(Config(usage_budget_usd=0.0)), "usage-budget")
    assert f is not None and f.severity == "medium"


def test_usage_budget_ok_when_set():
    assert check_usage_budget(Config(usage_budget_usd=100.0)) == []


# -- trace privacy (medium) --------------------------------------------------


def test_trace_privacy_medium_when_content_capture_on():
    f = _by_code(check_trace_privacy(Config(trace_capture_content=True)), "trace-privacy")
    assert f is not None and f.severity == "medium"


def test_trace_privacy_ok_when_off():
    assert check_trace_privacy(Config(trace_capture_content=False)) == []


# -- single user (medium, S1) ------------------------------------------------


def test_single_user_flags_empty_allowlist_with_open_replies():
    f = _by_code(check_single_user(Config(allowed_user_ids=[], respond_without_mention=True)),
                 "single-user")
    assert f is not None and f.severity in ("medium", "high")


def test_single_user_ok_when_allowlist_set():
    # An allowlist present -> the gate is bound; no actionable finding.
    findings = check_single_user(Config(allowed_user_ids=["123"], respond_without_mention=True))
    assert _by_code(findings, "single-user") is None


# -- secrets file mode (high) ------------------------------------------------


def test_secrets_mode_high_when_world_readable(tmp_path):
    env = tmp_path / ".env"
    env.write_text("IRIS_DISCORD_TOKEN=x", "utf-8")
    os.chmod(env, 0o644)  # group/world readable
    f = _by_code(check_secrets_mode(Config(), env_path=env, creds_path=tmp_path / "none.json"),
                 "secrets-mode")
    assert f is not None and f.severity == "high"


def test_secrets_mode_ok_when_600(tmp_path):
    env = tmp_path / ".env"
    env.write_text("IRIS_DISCORD_TOKEN=x", "utf-8")
    os.chmod(env, 0o600)
    assert check_secrets_mode(Config(), env_path=env, creds_path=tmp_path / "none.json") == []


# -- aggregation -------------------------------------------------------------


def test_run_audit_aggregates_and_is_nonempty_on_a_loose_config(monkeypatch):
    monkeypatch.delenv("IRIS_PUBLISH_DIR", raising=False)
    cfg = Config(restrict_builtin_tools=False, allowed_tools=["Bash", "mcp__publish__publish_video"],
                 usage_budget_usd=0.0, trace_capture_content=True)
    findings = run_audit(cfg)
    codes = _codes(findings)
    assert "chat-sandbox" in codes and "publish-dir" in codes and "usage-budget" in codes
    assert worst_severity(findings) == "critical"


def test_job_isolation_clean_for_a_normal_config(tmp_path, monkeypatch):
    from iris.audit import check_job_isolation
    monkeypatch.chdir(tmp_path)
    # A normal job (subagents) gets only built-ins, no iris MCP tools -> no finding.
    assert check_job_isolation(Config()) == []


def test_clock_gating_clean_for_a_normal_config():
    from iris.audit import check_clock_gating
    cfg = Config(allowed_tools=["mcp__jobs__schedule_job", "mcp__memory__recall"])
    # gate_self_starting strips the work-creators, so the check finds nothing.
    assert check_clock_gating(cfg) == []
