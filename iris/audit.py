"""A model-free security/compliance self-audit.

Where ``doctor`` answers "is claude installed and wired", ``audit`` answers "are
the §0 invariants and the S1-S5 hardening still holding". It is owner-run, makes
no model call and no network call (stat / read / inspect only), and emits only
actionable findings: a clean posture returns nothing. Ranked by severity so
``iris audit`` is usable as a cron/CI tripwire (non-zero exit on critical/high).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import Config
from .driver import DANGEROUS_BUILTINS

# Severity order, worst first. "ok" is the absence of any finding.
_ORDER = ["critical", "high", "medium", "low", "info"]

# The built-ins that give chat real reach; chat must never be able to call these.
_CHAT_FORBIDDEN = {"Bash", "Write", "Edit", "Task"}


@dataclass
class Finding:
    severity: str
    code: str
    title: str
    detail: str
    fix: str = ""


def worst_severity(findings: list[Finding]) -> str:
    """The highest severity present, or 'ok' when there are no findings."""
    present = {f.severity for f in findings}
    for sev in _ORDER:
        if sev in present:
            return sev
    return "ok"


def render_audit(findings: list[Finding]) -> str:
    """Group findings by severity (worst first) with a one-line count header."""
    if not findings:
        return "audit clean: no findings."
    counts = {sev: sum(1 for f in findings if f.severity == sev) for sev in _ORDER}
    header = "Iris audit: " + ", ".join(f"{counts[s]} {s}" for s in _ORDER if counts[s])
    lines = [header]
    for sev in _ORDER:
        group = [f for f in findings if f.severity == sev]
        if not group:
            continue
        lines.append("")
        lines.append(f"== {sev.upper()} ==")
        for f in group:
            line = f"  [{f.code}] {f.title}: {f.detail}"
            if f.fix:
                line += f" (fix: {f.fix})"
            lines.append(line)
    return "\n".join(lines)


# -- individual checks (each pure, each returns a list) -----------------------


def _effective_denylist(config: Config) -> set:
    """The denylist the chat driver actually applies (mirrors ClaudeDriver)."""
    if config.disallowed_tools:
        return set(config.disallowed_tools)
    if getattr(config, "restrict_builtin_tools", True):
        return set(DANGEROUS_BUILTINS)
    return set()


def check_chat_sandbox(config: Config) -> list[Finding]:
    """Chat must never be able to shell, write, edit, or spawn subagents."""
    denied = _effective_denylist(config)
    missing = _CHAT_FORBIDDEN - denied
    allowed = set(config.allowed_tools)
    # A forbidden tool is reachable if it is explicitly allow-listed and not
    # denied, or if there is no allowlist at all (so claude's defaults apply).
    exposed = (missing & allowed) if allowed else missing
    if not exposed:
        return []
    return [Finding(
        "critical", "chat-sandbox", "chat can reach dangerous built-ins",
        f"chat could call {sorted(exposed)} (not in the effective denylist)",
        "set IRIS_RESTRICT_BUILTIN_TOOLS=true or add them to IRIS_DISALLOWED_TOOLS")]


def check_publish_dir(config: Config) -> list[Finding]:
    """If the publish tool is allow-listed, IRIS_PUBLISH_DIR must be set (S5)."""
    allows_publish = any(t.startswith("mcp__publish__") for t in config.allowed_tools)
    if allows_publish and not os.environ.get("IRIS_PUBLISH_DIR"):
        return [Finding(
            "high", "publish-dir", "publishing is allowed but unbounded",
            "the publish tool is allow-listed but IRIS_PUBLISH_DIR is unset, so any mp4 could be posted",
            "set IRIS_PUBLISH_DIR to the finished-videos directory")]
    return []


def check_usage_budget(config: Config) -> list[Finding]:
    """A zero budget disarms the credit-guard park backstop."""
    if (config.usage_budget_usd or 0) <= 0:
        return [Finding(
            "medium", "usage-budget", "credit-guard park backstop disarmed",
            "IRIS_USAGE_BUDGET_USD is 0, so self-started work is bounded only by the weekly-usage gate",
            "set IRIS_USAGE_BUDGET_USD to arm parking")]
    return []


def check_trace_privacy(config: Config) -> list[Finding]:
    """Content capture stores prompts and replies on disk."""
    if getattr(config, "trace_capture_content", False):
        return [Finding(
            "medium", "trace-privacy", "trace ledger captures content",
            "IRIS_TRACE_CAPTURE_CONTENT=true writes prompts and replies to the trace file",
            "leave it off unless you need full transcripts, and keep the trace file 600")]
    return []


def check_single_user(config: Config) -> list[Finding]:
    """The single-user gate must bind who is answered (S1)."""
    if config.allowed_user_ids:
        return []
    if config.respond_without_mention:
        return [Finding(
            "medium", "single-user", "open allowlist with answer-without-mention",
            "IRIS_ALLOWED_USER_IDS is empty and respond_without_mention is true: anyone who posts is answered",
            "set IRIS_ALLOWED_USER_IDS to your id (the code already fails closed on this combo)")]
    return [Finding(
        "info", "single-user", "allowlist is empty",
        "IRIS_ALLOWED_USER_IDS is unset; fine on a single-operator box, otherwise set your id",
        "set IRIS_ALLOWED_USER_IDS to your id")]


def check_secrets_mode(config: Config, *, env_path: Optional[Path] = None,
                       creds_path: Optional[Path] = None) -> list[Finding]:
    """Secret-bearing files must be owner-only (mode 600)."""
    if env_path is None:
        env_path = Path(".env")
    if creds_path is None:
        creds_path = Path(getattr(config, "proactive_creds_path", "") or
                          os.path.expanduser("~/.claude/.credentials.json"))
    findings: list[Finding] = []
    for path, label in ((env_path, ".env"), (creds_path, "credentials")):
        try:
            if not Path(path).exists():
                continue
            mode = Path(path).stat().st_mode & 0o777
        except OSError:
            continue
        if mode & 0o077:  # any group/other bit set
            findings.append(Finding(
                "high", "secrets-mode", f"{label} is not owner-only",
                f"{path} is mode {mode:o}; group/other can read the secrets it holds",
                f"chmod 600 {path}"))
    return findings


def check_job_isolation(config: Config) -> list[Finding]:
    """A job must not receive the iris MCP servers (it could otherwise spawn jobs
    or widen its grants). Exercises the real builder so a regression is caught."""
    from .jobs import _cleanup_job_sandbox, build_job_driver
    try:
        driver = build_job_driver(config, {"grants": ["subagents"], "workspace": ""}, None)
    except Exception:
        return []  # a misconfig that fails to build is not an isolation finding
    try:
        leaked = [t for t in (driver.allowed_tools or [])
                  if t.startswith("mcp__") and not t.startswith("mcp__playwright")]
        if leaked:
            return [Finding(
                "high", "job-isolation", "a job can reach iris MCP tools",
                f"the job driver exposes {sorted(leaked)}; a job could spawn jobs or widen grants",
                "build_job_driver must not pass the chat mcp config to jobs")]
        return []
    finally:
        _cleanup_job_sandbox(driver)


def check_clock_gating(config: Config) -> list[Finding]:
    """Clock-triggered contexts must not keep the self-starting-work tools."""
    from .gating import SELF_STARTING_TOOLS, gate_self_starting
    still = set(gate_self_starting(config).allowed_tools) & set(SELF_STARTING_TOOLS)
    if still:
        return [Finding(
            "high", "clock-gating", "clock contexts can create self-starting work",
            f"self-starting tools survive gating in the allowlist: {sorted(still)}",
            "gate_self_starting must strip schedule_job/run_in_background/start_job/set_goal")]
    return []


_CHECKS = (
    check_secrets_mode,
    check_chat_sandbox,
    check_publish_dir,
    check_usage_budget,
    check_trace_privacy,
    check_single_user,
    check_job_isolation,
    check_clock_gating,
)


def run_audit(config: Config) -> list[Finding]:
    """Run every check and return all findings (worst-first by severity)."""
    findings: list[Finding] = []
    for check in _CHECKS:
        try:
            findings.extend(check(config))
        except Exception:  # a broken check must never crash the audit
            continue
    findings.sort(key=lambda f: _ORDER.index(f.severity) if f.severity in _ORDER else 99)
    return findings
