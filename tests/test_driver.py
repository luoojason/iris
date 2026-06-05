"""Driver tests. No real ``claude`` is invoked: a fake runner is injected."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from iris.driver import ClaudeDriver, ClaudeResult


@dataclass
class FakeProc:
    returncode: int
    stdout: str
    stderr: str = ""


def make_runner(responses, record=None):
    """Return a runner that yields queued FakeProcs (or raises) per call."""
    queue = list(responses)

    def runner(cmd, timeout, prompt):
        if record is not None:
            record.append({"cmd": list(cmd), "prompt": prompt})
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return runner


def success_json(text="hello", session_id="sess-1", **extra):
    payload = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": text,
        "session_id": session_id,
        "total_cost_usd": 0.01,
        "duration_ms": 1234,
        "num_turns": 1,
        "modelUsage": {"claude-sonnet-4-6": {"inputTokens": 3}},
    }
    payload.update(extra)
    return json.dumps(payload)


def test_build_command_has_core_flags_and_no_prompt():
    d = ClaudeDriver(model="claude-sonnet-4-6", runner=make_runner([]))
    cmd = d.build_command()
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"
    assert "--resume" not in cmd


def test_build_command_resume_and_extras():
    d = ClaudeDriver(
        mcp_config="tools.json",
        append_system_prompt_file="persona.md",
        permission_mode="bypassPermissions",
        allowed_tools=["mcp__memory__recall", "Read"],
        add_dirs=["/srv/notes"],
        runner=make_runner([]),
    )
    cmd = d.build_command(session_id="abc")
    assert cmd[cmd.index("--resume") + 1] == "abc"
    assert cmd[cmd.index("--mcp-config") + 1] == "tools.json"
    assert "--strict-mcp-config" in cmd
    # persona is APPENDED, never replacing Claude Code's own system prompt
    assert cmd[cmd.index("--append-system-prompt-file") + 1] == "persona.md"
    assert "--system-prompt-file" not in cmd
    assert cmd[cmd.index("--permission-mode") + 1] == "bypassPermissions"
    i = cmd.index("--allowedTools")
    assert cmd[i + 1] == "mcp__memory__recall" and cmd[i + 2] == "Read"
    assert cmd[cmd.index("--add-dir") + 1] == "/srv/notes"


def test_dash_prefixed_prompt_goes_to_stdin_not_argv():
    record = []
    d = ClaudeDriver(runner=make_runner([FakeProc(0, success_json())], record=record))
    d.run("- buy milk")
    cmd = record[0]["cmd"]
    assert "- buy milk" not in cmd          # never an argv token (would parse as a flag)
    assert record[0]["prompt"] == "- buy milk"   # delivered on stdin


def test_successful_turn_parses_fields():
    d = ClaudeDriver(runner=make_runner([FakeProc(0, success_json(text="hi back", session_id="s9"))]))
    result = d.run("hello")
    assert isinstance(result, ClaudeResult)
    assert result.is_error is False
    assert result.text == "hi back"
    assert result.session_id == "s9"
    assert result.cost_usd == 0.01
    assert result.model == "claude-sonnet-4-6"
    assert result.num_turns == 1


def test_resume_passes_existing_session():
    record = []
    d = ClaudeDriver(runner=make_runner([FakeProc(0, success_json())], record=record))
    d.run("hello", session_id="prev-session")
    cmd = record[0]["cmd"]
    assert cmd[cmd.index("--resume") + 1] == "prev-session"


def test_error_subtype_marks_error():
    bad = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "boom", "session_id": "s"})
    d = ClaudeDriver(max_retries=0, runner=make_runner([FakeProc(0, bad)]))
    result = d.run("hello")
    assert result.is_error is True
    assert result.error


def test_retries_on_rate_limit_then_succeeds():
    slept = []
    rate = json.dumps({"type": "result", "subtype": "success", "is_error": True,
                       "api_error_status": 429, "result": "rate_limit_error", "session_id": "s"})
    runner = make_runner([FakeProc(0, rate), FakeProc(0, success_json(text="recovered"))])
    d = ClaudeDriver(max_retries=2, retry_base_delay=0.0, runner=runner, sleep=slept.append)
    result = d.run("hello")
    assert result.is_error is False
    assert result.text == "recovered"
    assert len(slept) == 1


def test_timeout_is_retried_then_reported():
    slept = []
    runner = make_runner([subprocess.TimeoutExpired("claude", 1), subprocess.TimeoutExpired("claude", 1)])
    d = ClaudeDriver(max_retries=1, retry_base_delay=0.0, runner=runner, sleep=slept.append)
    result = d.run("hello")
    assert result.is_error is True
    assert "timed out" in (result.error or "")
    assert len(slept) == 1


def test_parses_json_after_leading_log_noise():
    noisy = "warning: something\n" + success_json(text="clean", session_id="s2")
    d = ClaudeDriver(runner=make_runner([FakeProc(0, noisy)]))
    result = d.run("hello")
    assert result.is_error is False
    assert result.text == "clean"
    assert result.session_id == "s2"


def test_unparseable_output_is_an_error():
    d = ClaudeDriver(max_retries=0, runner=make_runner([FakeProc(1, "", "fatal: not logged in")]))
    result = d.run("hello")
    assert result.is_error is True
    assert "not logged in" in (result.error or "")


def test_context_tokens_sums_fresh_and_cached_input():
    usage = {
        "input_tokens": 18311,
        "cache_read_input_tokens": 16184,
        "cache_creation_input_tokens": 4850,
        "output_tokens": 17,
    }
    d = ClaudeDriver(runner=make_runner([FakeProc(0, success_json(usage=usage))]))
    result = d.run("hello")
    assert result.context_tokens == 18311 + 16184 + 4850  # output tokens excluded


def test_context_tokens_absent_when_no_usage():
    d = ClaudeDriver(runner=make_runner([FakeProc(0, success_json())]))
    result = d.run("hello")
    assert result.context_tokens is None
