"""Per-job driver policy tests: pure build_command argv assertions, no claude."""

from __future__ import annotations

from iris.driver import DANGEROUS_BUILTINS, ClaudeDriver
from iris.jobs import build_job_driver


def make_job(**overrides):
    job = {
        "id": 3,
        "title": "refactor parser",
        "prompt": "refactor the parser",
        "status": "pending",
        "model": "",
        "timeout_s": 1800,
        "grants": [],
    }
    job.update(overrides)
    return job


def denied_tools(cmd):
    """The --disallowedTools tail of a build_command argv."""
    i = cmd.index("--disallowedTools")
    tail = cmd[i + 1:]
    for j, token in enumerate(tail):
        if token.startswith("--"):
            return tail[:j]
    return tail


def test_granted_task_is_carved_out_while_the_rest_stay_denied():
    base = ClaudeDriver()
    d = build_job_driver(base, make_job(grants=["Task"]), grant_ceiling=("Task",))
    denied = denied_tools(d.build_command())
    assert "Task" not in denied
    assert "Bash" in denied and "Write" in denied and "Edit" in denied
    assert "NotebookEdit" in denied and "KillShell" in denied and "BashOutput" in denied


def test_empty_grants_keep_the_full_denylist():
    d = build_job_driver(ClaudeDriver(), make_job(grants=[]), grant_ceiling=("Task",))
    assert tuple(denied_tools(d.build_command())) == DANGEROUS_BUILTINS


def test_grants_beyond_the_operator_ceiling_are_ignored():
    job = make_job(grants=["Task", "Bash", "Write"])
    d = build_job_driver(ClaudeDriver(), job, grant_ceiling=("Task",))
    denied = denied_tools(d.build_command())
    assert "Task" not in denied  # inside the ceiling
    assert "Bash" in denied and "Write" in denied  # asked for, not granted


def test_job_timeout_model_and_preamble_come_from_the_job():
    base = ClaudeDriver(model="claude-sonnet-4-6", timeout=300.0)
    job = make_job(model="claude-haiku-4-5", timeout_s=900)
    d = build_job_driver(base, job, grant_ceiling=("Task",))
    assert d.timeout == 900.0
    cmd = d.build_command()
    assert cmd[cmd.index("--model") + 1] == "claude-haiku-4-5"
    from iris.jobs import JOB_PREAMBLE
    assert cmd[cmd.index("--append-system-prompt") + 1] == JOB_PREAMBLE


def test_empty_job_model_keeps_the_base_drivers_model():
    base = ClaudeDriver(model="claude-sonnet-4-6")
    d = build_job_driver(base, make_job(model=""), grant_ceiling=("Task",))
    cmd = d.build_command()
    assert cmd[cmd.index("--model") + 1] == "claude-sonnet-4-6"


def test_fresh_session_never_resumes():
    d = build_job_driver(ClaudeDriver(), make_job(grants=["Task"]), grant_ceiling=("Task",))
    assert "--resume" not in d.build_command()
    assert "--resume" not in d.build_command(stream=True)


def test_plain_chat_driver_still_denies_task_by_default():
    # PIN: re-enabling Task is a job-driver privilege only; the interactive
    # chat driver's default denylist must keep blocking subagent spawning.
    denied = denied_tools(ClaudeDriver().build_command())
    assert "Task" in denied
    assert "Bash" in denied and "Write" in denied


def test_building_a_job_driver_leaves_the_base_driver_untouched():
    base = ClaudeDriver(model="claude-sonnet-4-6", timeout=300.0)
    build_job_driver(base, make_job(grants=["Task"], model="claude-haiku-4-5",
                                    timeout_s=900), grant_ceiling=("Task",))
    assert base.disallowed_tools is None  # still the implicit default denylist
    assert base.model == "claude-sonnet-4-6"
    assert base.timeout == 300.0
    assert base.append_system_prompt is None
    assert "Task" in denied_tools(base.build_command())
