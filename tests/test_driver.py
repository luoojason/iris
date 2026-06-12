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


def test_timeout_is_not_retried_by_default():
    slept = []
    runner = make_runner([subprocess.TimeoutExpired("claude", 1)])
    d = ClaudeDriver(runner=runner, sleep=slept.append)  # timeout_max_retries defaults to 0
    result = d.run("hello")
    assert result.is_error is True
    assert "timed out" in (result.error or "")
    assert slept == []  # reported at once, no minutes-long retry storm


def test_timeout_retried_when_configured():
    slept = []
    runner = make_runner([subprocess.TimeoutExpired("claude", 1), subprocess.TimeoutExpired("claude", 1)])
    d = ClaudeDriver(timeout_max_retries=1, retry_base_delay=0.0, runner=runner, sleep=slept.append)
    result = d.run("hello")
    assert result.is_error is True
    assert "timed out" in (result.error or "")
    assert len(slept) == 1


def test_default_denylist_blocks_dangerous_builtins():
    d = ClaudeDriver(allowed_tools=["mcp__memory__recall"], runner=make_runner([]))
    cmd = d.build_command()
    i = cmd.index("--disallowedTools")
    denied = cmd[i + 1:]
    assert "Bash" in denied and "Write" in denied and "Task" in denied
    # read-only and advertised web tools stay available; only reach is denied
    assert "Read" not in denied
    assert "WebFetch" not in denied and "WebSearch" not in denied


def test_explicit_disallowed_tools_take_over():
    d = ClaudeDriver(disallowed_tools=["Bash"], runner=make_runner([]))
    cmd = d.build_command()
    i = cmd.index("--disallowedTools")
    assert cmd[i + 1] == "Bash"
    assert "Write" not in cmd[i + 1:]  # the default set is not also appended


def test_restrict_builtin_tools_can_be_disabled():
    d = ClaudeDriver(restrict_builtin_tools=False, runner=make_runner([]))
    assert "--disallowedTools" not in d.build_command()


def test_terminal_errors_are_not_retried():
    slept = []
    bad = json.dumps({"type": "result", "subtype": "success", "is_error": True,
                      "api_error_status": 401, "result": "authentication_error", "session_id": "s"})
    d = ClaudeDriver(max_retries=2, retry_base_delay=0.0, runner=make_runner([FakeProc(0, bad)]), sleep=slept.append)
    result = d.run("hello")
    assert result.is_error is True
    assert slept == []  # 401 is permanent, not retried


def test_credit_exhaustion_is_not_retried():
    slept = []
    broke = json.dumps({"type": "result", "subtype": "success", "is_error": True,
                        "result": "Your credit balance is too low", "session_id": "s"})
    d = ClaudeDriver(max_retries=2, retry_base_delay=0.0, runner=make_runner([FakeProc(0, broke)]), sleep=slept.append)
    result = d.run("hello")
    assert result.is_error is True
    assert slept == []  # surfaces immediately instead of hiding behind backoff


def test_child_env_drops_iris_and_anthropic_secrets(monkeypatch):
    from iris.driver import _child_env
    monkeypatch.setenv("IRIS_DISCORD_TOKEN", "bot-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "oauth-leak")
    monkeypatch.setenv("PATH_THROUGH", "fine")
    env = _child_env(disable_auto_memory=True)
    assert "IRIS_DISCORD_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["PATH_THROUGH"] == "fine"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


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


def test_default_denylist_blocks_the_agent_alias():
    """Newer claude CLIs expose the subagent tool as Agent as well as Task.

    Denying only Task leaves subagent spawning reachable under the alias, so
    the default denylist must carry both names.
    """
    d = ClaudeDriver(runner=make_runner([]))
    cmd = d.build_command()
    i = cmd.index("--disallowedTools")
    denied = cmd[i + 1:]
    assert "Task" in denied and "Agent" in denied


class _FakePopen:
    """Captures Popen kwargs; yields one canned success result."""

    def __init__(self, cmd, **kwargs):
        _FakePopen.captured = dict(kwargs)
        self.returncode = 0
        self.pid = 4242

    def communicate(self, input=None, timeout=None):
        return ('{"result": "ok", "session_id": "s"}', "")


def test_subprocess_runs_in_the_configured_cwd(monkeypatch):
    import iris.driver as drv

    monkeypatch.setattr(drv.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(drv.shutil, "which", lambda name: "/usr/bin/claude")
    d = ClaudeDriver(cwd="/some/dir")
    result = d.run("hi")
    assert not result.is_error
    assert _FakePopen.captured["cwd"] == "/some/dir"


def test_subprocess_cwd_defaults_to_inherited(monkeypatch):
    import iris.driver as drv

    monkeypatch.setattr(drv.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(drv.shutil, "which", lambda name: "/usr/bin/claude")
    ClaudeDriver().run("hi")
    assert _FakePopen.captured["cwd"] is None


def test_child_pid_callback_sees_the_spawned_pid(monkeypatch):
    import iris.driver as drv

    monkeypatch.setattr(drv.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(drv.shutil, "which", lambda name: "/usr/bin/claude")
    pids = []
    d = ClaudeDriver(child_pid_callback=pids.append)
    d.run("hi")
    assert pids == [4242]


def test_child_pid_callback_errors_do_not_break_the_turn(monkeypatch):
    import iris.driver as drv

    monkeypatch.setattr(drv.subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(drv.shutil, "which", lambda name: "/usr/bin/claude")

    def explode(pid):
        raise RuntimeError("callback bug")

    result = ClaudeDriver(child_pid_callback=explode).run("hi")
    assert not result.is_error


# -- standing orders ----------------------------------------------------------


def test_standing_orders_content_is_appended(tmp_path):
    orders = tmp_path / "orders.md"
    orders.write_text("Always answer in metric.", encoding="utf-8")
    d = ClaudeDriver(standing_orders_file=str(orders), runner=make_runner([]))
    cmd = d.build_command()
    assert cmd[cmd.index("--append-system-prompt") + 1] == "Always answer in metric."


def test_standing_orders_reread_on_every_command(tmp_path):
    # Edits take effect on the next turn with no restart.
    orders = tmp_path / "orders.md"
    orders.write_text("rule one", encoding="utf-8")
    d = ClaudeDriver(standing_orders_file=str(orders), runner=make_runner([]))
    first = d.build_command()
    orders.write_text("rule two", encoding="utf-8")
    second = d.build_command()
    assert first[first.index("--append-system-prompt") + 1] == "rule one"
    assert second[second.index("--append-system-prompt") + 1] == "rule two"


def test_standing_orders_concatenate_after_static_append(tmp_path):
    orders = tmp_path / "orders.md"
    orders.write_text("orders text", encoding="utf-8")
    d = ClaudeDriver(
        append_system_prompt="static text",
        standing_orders_file=str(orders),
        runner=make_runner([]),
    )
    cmd = d.build_command()
    assert cmd.count("--append-system-prompt") == 1  # one flag, merged value
    value = cmd[cmd.index("--append-system-prompt") + 1]
    assert value.startswith("static text")
    assert value.endswith("orders text")


def test_standing_orders_missing_or_blank_file_is_skipped(tmp_path):
    d = ClaudeDriver(standing_orders_file=str(tmp_path / "absent.md"), runner=make_runner([]))
    assert "--append-system-prompt" not in d.build_command()
    blank = tmp_path / "blank.md"
    blank.write_text("  \n\n", encoding="utf-8")
    d2 = ClaudeDriver(standing_orders_file=str(blank), runner=make_runner([]))
    assert "--append-system-prompt" not in d2.build_command()


def test_standing_orders_apply_to_the_stream_transport_too(tmp_path):
    # Both transports must stay in lockstep, hardening and persona alike.
    orders = tmp_path / "orders.md"
    orders.write_text("rule", encoding="utf-8")
    d = ClaudeDriver(standing_orders_file=str(orders), runner=make_runner([]))
    cmd = d.build_command(stream=True)
    assert cmd[cmd.index("--append-system-prompt") + 1] == "rule"


# -- system prompt extra supplier ---------------------------------------------


def test_system_prompt_extra_is_merged_last(tmp_path):
    orders = tmp_path / "orders.md"
    orders.write_text("rules first", encoding="utf-8")
    d = ClaudeDriver(
        standing_orders_file=str(orders),
        system_prompt_extra=lambda: "digest last",
        runner=make_runner([]),
    )
    cmd = d.build_command()
    value = cmd[cmd.index("--append-system-prompt") + 1]
    assert value.index("rules first") < value.index("digest last")
    assert cmd.count("--append-system-prompt") == 1


def test_system_prompt_extra_failure_never_breaks_a_turn():
    def boom():
        raise RuntimeError("store unreadable")

    d = ClaudeDriver(system_prompt_extra=boom, runner=make_runner([]))
    cmd = d.build_command()  # must not raise
    assert "--append-system-prompt" not in cmd
