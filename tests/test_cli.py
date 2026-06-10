"""Smoke test that the `python -m iris` entry point actually runs.

Unit tests import modules directly and would not catch a missing __main__.py,
so this runs the module the way a user does.
"""

from __future__ import annotations

import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_module_help_runs():
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-m", "iris", "--help"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    for command in ("discord", "telegram", "chat", "doctor", "watch"):
        assert command in result.stdout


def test_watch_without_command_errors():
    from iris.cli import main
    assert main(["watch"]) == 2  # no command after watch -> usage, exit 2


def test_watch_runs_command_and_returns_its_code(monkeypatch):
    from iris.cli import main
    import iris.notify.watch_cmd as wc
    seen = {}

    def fake_watch(argv, config, **kwargs):
        seen["argv"] = argv
        seen["name"] = kwargs.get("name")
        return 7

    monkeypatch.setattr(wc, "watch", fake_watch)
    rc = main(["watch", "--name", "build", "--", "npm", "test"])
    assert rc == 7
    assert seen["argv"] == ["npm", "test"]
    assert seen["name"] == "build"


def test_doctor_reports_missing_binary():
    from iris.cli import doctor
    from iris.config import Config

    # A bogus binary name: doctor must fail cleanly without any network call.
    rc = doctor(Config(claude_bin="iris-no-such-claude-binary"), probe=False)
    assert rc == 1


# -- iris jobs ----------------------------------------------------------------
# Pure registry operations against a tmp store: no model call anywhere.


def run_jobs(monkeypatch, tmp_path, argv):
    from iris.cli import main

    monkeypatch.setenv("IRIS_JOBS_FILE", str(tmp_path / "jobs.json"))
    return main(["jobs", *argv])


def jobs_store(tmp_path):
    from iris.jobs import JobStore

    return JobStore(tmp_path / "jobs.json")


def test_jobs_spawn_queues_a_job_with_its_options(monkeypatch, tmp_path, capsys):
    rc = run_jobs(monkeypatch, tmp_path, [
        "spawn", "audit", "the", "deps",
        "--title", "deps audit", "--model", "claude-haiku-4-5",
        "--timeout-minutes", "10", "--grants", "Task",
    ])

    assert rc == 0
    assert "Job #1 queued: deps audit" in capsys.readouterr().out
    job = jobs_store(tmp_path).get(1)
    assert job["prompt"] == "audit the deps"
    assert job["title"] == "deps audit"
    assert job["model"] == "claude-haiku-4-5"
    assert job["timeout_s"] == 600
    assert job["grants"] == ["Task"]
    assert job["status"] == "pending"


def test_jobs_spawn_defaults_the_title_to_the_prompts_first_line(monkeypatch, tmp_path, capsys):
    rc = run_jobs(monkeypatch, tmp_path, ["spawn", "summarize the repo"])

    assert rc == 0
    job = jobs_store(tmp_path).get(1)
    assert job["title"] == "summarize the repo"
    assert job["timeout_s"] == 1800  # store default budget


def test_jobs_spawn_rejects_an_unknown_grant(monkeypatch, tmp_path, capsys):
    rc = run_jobs(monkeypatch, tmp_path, ["spawn", "work", "--grants", "Sudo"])

    assert rc == 2
    assert "Sudo" in capsys.readouterr().out
    assert jobs_store(tmp_path).all() == []


def test_jobs_list_shows_jobs_newest_first(monkeypatch, tmp_path, capsys):
    store = jobs_store(tmp_path)
    store.add("first work", "first")
    store.add("second work", "second")

    rc = run_jobs(monkeypatch, tmp_path, ["list"])

    assert rc == 0
    out = capsys.readouterr().out
    lines = [l for l in out.splitlines() if l.startswith("#")]
    assert lines[0].startswith("#2 [pending")
    assert "second" in lines[0]
    assert lines[1].startswith("#1 [pending")


def test_jobs_list_empty_and_status_filter(monkeypatch, tmp_path, capsys):
    assert run_jobs(monkeypatch, tmp_path, ["list"]) == 0
    assert "No jobs." in capsys.readouterr().out

    jobs_store(tmp_path).add("work", "title")
    assert run_jobs(monkeypatch, tmp_path, ["list", "--status", "done"]) == 0
    assert "No done jobs." in capsys.readouterr().out


def test_jobs_show_prints_detail_and_fails_on_missing(monkeypatch, tmp_path, capsys):
    store = jobs_store(tmp_path)
    jid = store.add("deep work", "deep dive", model="claude-haiku-4-5", grants=["Task"])

    assert run_jobs(monkeypatch, tmp_path, ["show", str(jid)]) == 0
    out = capsys.readouterr().out
    assert "deep dive" in out
    assert "pending" in out
    assert "claude-haiku-4-5" in out
    assert "Task" in out

    assert run_jobs(monkeypatch, tmp_path, ["show", "99"]) == 1
    assert "No job #99." in capsys.readouterr().out


def test_jobs_show_surfaces_a_failure_error(monkeypatch, tmp_path, capsys):
    store = jobs_store(tmp_path)
    jid = store.add("doomed", "doomed")
    store.update(jid, status="failed", result={"text": "", "error": "exploded"})

    assert run_jobs(monkeypatch, tmp_path, ["show", str(jid)]) == 0
    assert "exploded" in capsys.readouterr().out


def test_jobs_cancel_pending_job(monkeypatch, tmp_path, capsys):
    store = jobs_store(tmp_path)
    jid = store.add("work", "title")

    assert run_jobs(monkeypatch, tmp_path, ["cancel", str(jid)]) == 0
    assert f"Cancelled job #{jid}." in capsys.readouterr().out
    assert store.get(jid)["status"] == "cancelled"


def test_jobs_without_a_subcommand_prints_usage(monkeypatch, tmp_path, capsys):
    assert run_jobs(monkeypatch, tmp_path, []) == 2
    assert "usage" in capsys.readouterr().out.lower()


# -- doctor: jobs wiring warnings ----------------------------------------------


def doctor_config(tmp_path, *, mcp=None, allowed=(), jobs_enabled=False):
    from iris.config import Config

    # sys.executable stands in for the claude binary: doctor only runs
    # `--version` with probe=False, which python answers locally.
    return Config(
        claude_bin=sys.executable,
        mcp_config=str(mcp) if mcp else None,
        allowed_tools=list(allowed),
        jobs_enabled=jobs_enabled,
        allowed_user_ids=["1"],
    )


def write_mcp(tmp_path, servers):
    import json

    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return path


def test_doctor_warns_when_jobs_server_has_no_allowlisted_tools(tmp_path, capsys):
    from iris.cli import doctor

    mcp = write_mcp(tmp_path, {"jobs": {"command": "python"}})
    rc = doctor(doctor_config(tmp_path, mcp=mcp, allowed=["mcp__memory__recall"]),
                probe=False)

    assert rc == 0
    out = capsys.readouterr().out
    assert "WARNING" in out
    for tool in ("mcp__jobs__spawn_job", "mcp__jobs__list_jobs", "mcp__jobs__job_status",
                 "mcp__jobs__cancel_job", "mcp__jobs__job_result"):
        assert tool in out


def test_doctor_stays_quiet_when_a_jobs_tool_is_allowlisted(tmp_path, capsys):
    from iris.cli import doctor

    mcp = write_mcp(tmp_path, {"jobs": {"command": "python"}})
    doctor(doctor_config(tmp_path, mcp=mcp, allowed=["mcp__jobs__spawn_job"]),
           probe=False)

    assert "mcp__jobs__cancel_job" not in capsys.readouterr().out


def test_doctor_warns_when_jobs_enabled_but_no_jobs_server(tmp_path, capsys):
    from iris.cli import doctor

    mcp = write_mcp(tmp_path, {"memory": {"command": "python"}})
    rc = doctor(doctor_config(tmp_path, mcp=mcp, allowed=["mcp__memory__recall"],
                              jobs_enabled=True), probe=False)

    assert rc == 0
    out = capsys.readouterr().out
    assert "IRIS_JOBS" in out
    assert "cannot spawn jobs" in out


def test_doctor_warns_when_jobs_enabled_with_no_mcp_config_at_all(tmp_path, capsys):
    from iris.cli import doctor

    doctor(doctor_config(tmp_path, jobs_enabled=True), probe=False)

    assert "cannot spawn jobs" in capsys.readouterr().out


def test_doctor_no_jobs_warnings_when_everything_lines_up(tmp_path, capsys):
    from iris.cli import doctor

    mcp = write_mcp(tmp_path, {"jobs": {"command": "python"}})
    doctor(doctor_config(tmp_path, mcp=mcp, allowed=["mcp__jobs__spawn_job"],
                         jobs_enabled=True), probe=False)

    out = capsys.readouterr().out
    assert "cannot spawn jobs" not in out
