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


# -- iris usage -----------------------------------------------------------------
# Pure file arithmetic over a tmp metrics JSONL: no model call, no network.


def metric(ts, cost, conversation_id="discord:1", model="claude-sonnet-4-6", **over):
    rec = {"ts": ts, "conversation_id": conversation_id, "model": model,
           "cost_usd": cost, "context_tokens": 1000, "is_error": False}
    rec.update(over)
    return rec


def write_metrics(path, records):
    import json

    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")


def usage_args(**over):
    import argparse

    fields = dict(period="month", as_json=False)
    fields.update(over)
    return argparse.Namespace(**fields)


def test_usage_without_a_metrics_file_is_friendly(capsys):
    from iris.cli import usage
    from iris.config import Config

    rc = usage(Config(metrics_file=""), usage_args())
    assert rc == 0
    assert "IRIS_METRICS_FILE" in capsys.readouterr().out


def test_usage_with_a_missing_file_is_friendly(tmp_path, capsys):
    from iris.cli import usage
    from iris.config import Config

    rc = usage(Config(metrics_file=str(tmp_path / "absent.jsonl")), usage_args())
    assert rc == 0
    assert "No metrics recorded" in capsys.readouterr().out


def test_usage_renders_the_shared_summary_for_the_month(tmp_path, capsys):
    from datetime import datetime

    from iris.cli import usage
    from iris.config import Config

    path = tmp_path / "m.jsonl"
    write_metrics(path, [
        metric(datetime(2026, 6, 5).timestamp(), 0.20, model="claude-opus-4-6"),
        metric(datetime(2026, 6, 10).timestamp(), 0.05, conversation_id="job:3"),
        metric(datetime(2026, 5, 20).timestamp(), 9.99),  # last month: excluded
    ])
    rc = usage(Config(metrics_file=str(path)), usage_args(),
               now=datetime(2026, 6, 16).timestamp())

    out = capsys.readouterr().out
    assert rc == 0
    assert "spend: $0.25 (2 turns)" in out
    assert "claude-opus-4-6: $0.20" in out
    assert "job: $0.05" in out  # job spend separated via the transport breakdown
    assert "used" not in out    # no credit configured: no credit lines


def test_usage_period_day_narrows_the_window(tmp_path, capsys):
    from datetime import datetime

    from iris.cli import usage
    from iris.config import Config

    path = tmp_path / "m.jsonl"
    write_metrics(path, [
        metric(datetime(2026, 6, 16, 1).timestamp(), 0.10),
        metric(datetime(2026, 6, 15).timestamp(), 5.00),  # yesterday: excluded
    ])
    rc = usage(Config(metrics_file=str(path)), usage_args(period="day"),
               now=datetime(2026, 6, 16, 12).timestamp())

    assert rc == 0
    assert "spend: $0.10 (1 turns)" in capsys.readouterr().out


def test_usage_credit_and_projection_lines_when_a_credit_is_set(tmp_path, capsys):
    from datetime import datetime

    from iris.cli import usage
    from iris.config import Config

    path = tmp_path / "m.jsonl"
    write_metrics(path, [metric(datetime(2026, 6, 5).timestamp(), 40.0)])
    rc = usage(Config(metrics_file=str(path), monthly_credit=100.0), usage_args(),
               now=datetime(2026, 6, 16).timestamp())  # half of June elapsed

    out = capsys.readouterr().out
    assert rc == 0
    assert "credit: $40.00 of $100.00 (40.0% used)" in out
    assert "projected month end: $80.00" in out


def test_usage_credit_lines_stay_off_outside_the_month_period(tmp_path, capsys):
    # Day spend against a monthly credit would mislead; month period only.
    from datetime import datetime

    from iris.cli import usage
    from iris.config import Config

    path = tmp_path / "m.jsonl"
    write_metrics(path, [metric(datetime(2026, 6, 16, 1).timestamp(), 40.0)])
    usage(Config(metrics_file=str(path), monthly_credit=100.0),
          usage_args(period="day"), now=datetime(2026, 6, 16, 12).timestamp())

    assert "used" not in capsys.readouterr().out


def test_usage_json_dumps_the_summary_dict(tmp_path, capsys):
    import json
    from datetime import datetime

    from iris.cli import usage
    from iris.config import Config

    path = tmp_path / "m.jsonl"
    write_metrics(path, [
        metric(datetime(2026, 6, 5).timestamp(), 0.20),
        metric(datetime(2026, 6, 10).timestamp(), 0.05, conversation_id="job:3"),
    ])
    rc = usage(Config(metrics_file=str(path)), usage_args(as_json=True),
               now=datetime(2026, 6, 16).timestamp())

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["turns"] == 2
    assert abs(data["total_cost"] - 0.25) < 1e-9
    assert abs(data["by_transport"]["job"] - 0.05) < 1e-9


def test_usage_json_includes_the_credit_keys_for_the_month(tmp_path, capsys):
    # The text rendering shows credit/percent/projection for the month; the
    # JSON payload must carry the same story for scripts.
    import json
    from datetime import datetime

    from iris.cli import usage
    from iris.config import Config

    path = tmp_path / "m.jsonl"
    write_metrics(path, [metric(datetime(2026, 6, 5).timestamp(), 40.0)])
    rc = usage(Config(metrics_file=str(path), monthly_credit=100.0),
               usage_args(as_json=True),
               now=datetime(2026, 6, 16).timestamp())  # half of June elapsed

    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["credit"] == 100.0
    assert abs(data["percent_used"] - 40.0) < 1e-9
    assert abs(data["projected_month_end"] - 80.0) < 1e-9


def test_usage_json_credit_keys_stay_off_outside_the_month_or_credit(tmp_path, capsys):
    # Day spend against a monthly credit would mislead, and without a credit
    # there is nothing to measure against: the keys are simply absent.
    import json
    from datetime import datetime

    from iris.cli import usage
    from iris.config import Config

    path = tmp_path / "m.jsonl"
    write_metrics(path, [metric(datetime(2026, 6, 16, 1).timestamp(), 40.0)])

    usage(Config(metrics_file=str(path), monthly_credit=100.0),
          usage_args(period="day", as_json=True),
          now=datetime(2026, 6, 16, 12).timestamp())
    day_data = json.loads(capsys.readouterr().out)

    usage(Config(metrics_file=str(path)), usage_args(as_json=True),
          now=datetime(2026, 6, 16, 12).timestamp())
    no_credit_data = json.loads(capsys.readouterr().out)

    for data in (day_data, no_credit_data):
        assert "credit" not in data
        assert "percent_used" not in data
        assert "projected_month_end" not in data


def test_usage_via_main_never_touches_network_or_subprocess(monkeypatch, tmp_path, capsys):
    import socket
    import subprocess as sp
    import time

    from iris.cli import main

    def explode(*args, **kwargs):
        raise AssertionError("iris usage must not open sockets or spawn processes")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(sp, "Popen", explode)
    path = tmp_path / "m.jsonl"
    write_metrics(path, [metric(time.time(), 0.30)])
    monkeypatch.setenv("IRIS_METRICS_FILE", str(path))
    monkeypatch.delenv("IRIS_MONTHLY_CREDIT", raising=False)
    monkeypatch.delenv("IRIS_SKILLS_DIR", raising=False)

    assert main(["usage"]) == 0
    assert "spend: $0.30" in capsys.readouterr().out


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


def test_jobs_spawn_clamps_the_timeout_like_the_server(monkeypatch, tmp_path, capsys):
    rc = run_jobs(monkeypatch, tmp_path, [
        "spawn", "long haul", "--timeout-minutes", "999",
    ])

    assert rc == 0
    assert jobs_store(tmp_path).get(1)["timeout_s"] == 240 * 60


def test_doctor_warns_when_the_jobs_registry_paths_diverge(tmp_path, capsys):
    from iris.cli import doctor

    mcp = write_mcp(tmp_path, {"jobs": {"command": "python",
                                        "env": {"IRIS_JOBS_FILE": str(tmp_path / "other.json")}}})
    rc = doctor(doctor_config(tmp_path, mcp=mcp, allowed=["mcp__jobs__spawn_job"]),
                probe=False)

    assert rc == 0
    assert "DIFFERENT registry" in capsys.readouterr().out


def test_doctor_quiet_when_jobs_registry_paths_agree(tmp_path, capsys):
    from iris.cli import doctor

    mcp = write_mcp(tmp_path, {"jobs": {"command": "python",
                                        "env": {"IRIS_JOBS_FILE": "iris-jobs.json"}}})
    doctor(doctor_config(tmp_path, mcp=mcp, allowed=["mcp__jobs__spawn_job"]),
           probe=False)

    assert "DIFFERENT registry" not in capsys.readouterr().out


def test_doctor_warns_when_usage_server_cannot_see_the_metrics_file(tmp_path, capsys):
    from iris.cli import doctor

    mcp = write_mcp(tmp_path, {"usage": {"command": "python"}})
    config = doctor_config(tmp_path, mcp=mcp, allowed=["mcp__usage__usage_summary"])
    config.metrics_file = str(tmp_path / "metrics.jsonl")
    doctor(config, probe=False)

    assert "IRIS_METRICS_FILE" in capsys.readouterr().out
