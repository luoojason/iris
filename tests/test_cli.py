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


def test_usage_error_does_not_load_dotenv(tmp_path, monkeypatch):
    """A usage error must exit before .env is read into the process env.

    Otherwise any in-process main() call (tests, embedding code) silently
    pollutes os.environ with whatever .env sits in the current directory.
    """
    from iris.cli import main

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("IRIS_CANARY_TOKEN=leaked\n", encoding="utf-8")
    monkeypatch.delenv("IRIS_CANARY_TOKEN", raising=False)
    assert main(["watch"]) == 2
    assert "IRIS_CANARY_TOKEN" not in os.environ


def test_watch_runs_command_and_returns_its_code(tmp_path, monkeypatch):
    from iris.cli import main
    import iris.notify.watch_cmd as wc
    seen = {}

    def fake_watch(argv, config, **kwargs):
        seen["argv"] = argv
        seen["name"] = kwargs.get("name")
        return 7

    # Run away from the repo root so a real .env there is never read into
    # this process's environment (main() loads .env from the cwd).
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wc, "watch", fake_watch)
    rc = main(["watch", "--name", "build", "--", "npm", "test"])
    assert rc == 7
    assert seen["argv"] == ["npm", "test"]
    assert seen["name"] == "build"


def test_watch_resume_flag_reaches_the_call(tmp_path, monkeypatch):
    from iris.cli import main
    import iris.notify.watch_cmd as wc
    seen = {}

    def fake_watch(argv, config, **kwargs):
        seen["resume"] = kwargs.get("resume")
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(wc, "watch", fake_watch)
    main(["watch", "--name", "build", "--fold", "--resume", "--", "true"])
    assert seen["resume"] is True


def test_doctor_reports_missing_binary():
    from iris.cli import doctor
    from iris.config import Config

    # A bogus binary name: doctor must fail cleanly without any network call.
    rc = doctor(Config(claude_bin="iris-no-such-claude-binary"), probe=False)
    assert rc == 1


def _fake_claude(tmp_path):
    """A stand-in 'claude' executable so doctor's local checks pass offline."""
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho fake-claude 0.0.0\n", encoding="utf-8")
    fake.chmod(0o755)
    return str(fake)


def test_doctor_warns_auto_resume_without_home_channel(tmp_path, capsys):
    from iris.cli import doctor
    from iris.config import Config

    rc = doctor(Config(claude_bin=_fake_claude(tmp_path), auto_resume=True,
                       home_channel=""), probe=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "auto-resume" in out.lower()
    assert "WARNING" in out


def test_doctor_reports_auto_resume_when_configured(tmp_path, capsys):
    from iris.cli import doctor
    from iris.config import Config

    rc = doctor(Config(claude_bin=_fake_claude(tmp_path), auto_resume=True,
                       home_channel="123"), probe=False)
    out = capsys.readouterr().out
    assert rc == 0
    line = [ln for ln in out.splitlines() if "auto-resume" in ln.lower()][0]
    assert "12" in line  # the per-day cap is surfaced


def test_doctor_reports_standing_orders(tmp_path, capsys):
    from iris.cli import doctor
    from iris.config import Config

    orders = tmp_path / "orders.md"
    orders.write_text("Always answer in metric.", encoding="utf-8")
    rc = doctor(
        Config(claude_bin=_fake_claude(tmp_path), standing_orders_file=str(orders)),
        probe=False,
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "standing orders" in out
    assert "WARNING" not in out.split("standing orders")[1].split("\n")[0]


def test_doctor_warns_on_oversize_standing_orders(tmp_path, capsys):
    # Every standing-orders byte is re-billed on every turn; flag a bloated file.
    from iris.cli import doctor
    from iris.config import Config

    orders = tmp_path / "orders.md"
    orders.write_text("x" * 3000, encoding="utf-8")
    doctor(
        Config(claude_bin=_fake_claude(tmp_path), standing_orders_file=str(orders)),
        probe=False,
    )
    out = capsys.readouterr().out
    assert "standing orders" in out
    assert "2KB" in out or "2 KB" in out


def test_doctor_flags_missing_standing_orders_file(tmp_path, capsys):
    from iris.cli import doctor
    from iris.config import Config

    doctor(
        Config(
            claude_bin=_fake_claude(tmp_path),
            standing_orders_file=str(tmp_path / "absent.md"),
        ),
        probe=False,
    )
    out = capsys.readouterr().out
    assert "standing orders" in out and "MISSING" in out


def test_reminders_tick_renders_followups_and_requeues_failures(tmp_path, monkeypatch, capsys):
    from iris import reminders as rmod
    from iris.cli import reminders_tick
    from iris.config import Config

    monkeypatch.chdir(tmp_path)  # keep tick state files out of the repo
    path = tmp_path / "r.json"
    store = rmod.ReminderStore(path)
    store.add(0.0, "the deploy", "c1", kind="followup", origin="model")
    store.add(0.0, "stand up", "c2")
    monkeypatch.setenv("IRIS_REMINDERS_FILE", str(path))

    sent = []

    def fake_send(channel_id, content, token):
        sent.append((channel_id, content))
        return channel_id != "c2"  # the plain one fails and must requeue

    monkeypatch.setattr(rmod, "send_discord_message", fake_send)
    rc = reminders_tick(Config(discord_token="tok", usage_file=str(tmp_path / "u.json"),
                               wakes_file=str(tmp_path / "w.json"),
                               wakes_state=str(tmp_path / "w.state.json")))
    assert rc == 0
    followup = [c for ch, c in sent if ch == "c1"][0]
    assert followup.startswith("Follow-up")
    requeued = store.all()
    assert len(requeued) == 1 and requeued[0]["text"] == "stand up"


def test_reminders_tick_runs_the_schedules_tick(tmp_path, monkeypatch, capsys):
    from iris.cli import reminders_tick
    from iris.config import Config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IRIS_REMINDERS_FILE", str(tmp_path / "r.json"))
    rc = reminders_tick(Config(discord_token="tok", usage_file=str(tmp_path / "u.json"),
                               wakes_file=str(tmp_path / "w.json"),
                               wakes_state=str(tmp_path / "w.state.json"),
                               schedules_file=str(tmp_path / "s.json")))
    out = capsys.readouterr().out
    assert rc == 0
    assert "schedules: off" in out  # gated off by default, but the hook runs


def test_schedule_cmd_add_list_remove(tmp_path, capsys):
    import argparse

    from iris.cli import schedule_cmd
    from iris.config import Config

    config = Config(schedules_file=str(tmp_path / "s.json"),
                    scheduled_jobs_enabled=True, jobs_enabled=True)

    def ns(**kw):
        return argparse.Namespace(**kw)

    rc = schedule_cmd(config, ns(schedule_action="add", title="briefing",
                                 at="+1h", every="every 1d",
                                 instructions="morning briefing", command="",
                                 grant="", workspace="", cap=None))
    assert rc == 0
    rc = schedule_cmd(config, ns(schedule_action="list"))
    out = capsys.readouterr().out
    assert rc == 0
    assert "briefing" in out and "#1" in out
    rc = schedule_cmd(config, ns(schedule_action="remove", rule_id=1))
    assert rc == 0
    schedule_cmd(config, ns(schedule_action="list"))
    assert "No schedules" in capsys.readouterr().out


def test_schedule_cmd_warns_when_the_flag_is_off(tmp_path, capsys):
    import argparse

    from iris.cli import schedule_cmd
    from iris.config import Config

    config = Config(schedules_file=str(tmp_path / "s.json"))
    rc = schedule_cmd(config, argparse.Namespace(
        schedule_action="add", title="t", at="+1h", every="",
        instructions="do", command="", grant="", workspace="", cap=None))
    out = capsys.readouterr().out
    assert rc == 0
    assert "IRIS_SCHEDULED_JOBS" in out  # recorded, but inert until enabled


def test_schedule_cmd_rejects_bad_rules(tmp_path, capsys):
    import argparse

    from iris.cli import schedule_cmd
    from iris.config import Config

    config = Config(schedules_file=str(tmp_path / "s.json"))
    rc = schedule_cmd(config, argparse.Namespace(
        schedule_action="add", title="t", at="+1h", every="",
        instructions="", command="", grant="", workspace="", cap=None))
    assert rc == 2


def test_heartbeat_cmd_shows_current_status(tmp_path, monkeypatch, capsys):
    import json

    from iris.cli import heartbeat_cmd
    from iris.config import Config

    (tmp_path / "hb.json").write_text(json.dumps([
        {"name": "site", "kind": "url_ok", "url": "https://e.com"}]), "utf-8")
    monkeypatch.setattr("iris.heartbeat.http_status", lambda url, timeout: 200)
    rc = heartbeat_cmd(Config(heartbeat_file=str(tmp_path / "hb.json")))
    out = capsys.readouterr().out
    assert rc == 0 and "site" in out and "ok" in out.lower()


def test_heartbeat_cmd_without_a_file(tmp_path, capsys):
    from iris.cli import heartbeat_cmd
    from iris.config import Config

    rc = heartbeat_cmd(Config(heartbeat_file=str(tmp_path / "nope.json")))
    assert rc == 0
    assert "IRIS_HEARTBEAT_FILE" in capsys.readouterr().out


def test_skills_cmd_pending_approve_reject(tmp_path, monkeypatch, capsys):
    import argparse

    from iris.cli import skills_cmd
    from iris.config import Config
    from iris.skills import SkillProposalStore, discover

    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # don't touch the real ~/.claude
    skills_dir = tmp_path / "myskills"
    pfile = tmp_path / "p.json"
    content = "---\nname: summarize\ndescription: Summarize docs into bullets\n---\nBody."
    SkillProposalStore(pfile).add("summarize", content, "often asked", kind="new", now=1.0)
    config = Config(skills_dir=str(skills_dir), skill_proposals_file=str(pfile))

    def ns(**kw):
        return argparse.Namespace(**kw)

    assert skills_cmd(config, ns(skills_action="pending")) == 0
    assert "summarize" in capsys.readouterr().out

    assert skills_cmd(config, ns(skills_action="approve", proposal_id=1)) == 0
    assert SkillProposalStore(pfile).get(1)["status"] == "approved"
    assert (skills_dir / "summarize" / "SKILL.md").read_text("utf-8") == content
    # the approved skill is now discoverable to claude
    assert "summarize" in dict(discover(str(tmp_path / "home")))

    assert skills_cmd(config, ns(skills_action="reject", proposal_id=99)) == 1  # no such id


def test_skills_cmd_approve_refuses_an_already_decided_proposal(tmp_path, monkeypatch, capsys):
    import argparse

    from iris.cli import skills_cmd
    from iris.config import Config
    from iris.skills import SkillProposalStore

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    pfile = tmp_path / "p.json"
    content = "---\nname: s\ndescription: d\n---\nbody"
    store = SkillProposalStore(pfile)
    store.add("s", content, "r", kind="new", now=1.0)
    store.transition(1, "rejected", now=2.0)
    config = Config(skills_dir=str(tmp_path / "myskills"), skill_proposals_file=str(pfile))
    rc = skills_cmd(config, argparse.Namespace(skills_action="approve", proposal_id=1))
    assert rc == 2  # a rejected proposal cannot be resurrected by approve
    assert "rejected" in capsys.readouterr().out.lower()
    assert SkillProposalStore(pfile).get(1)["status"] == "rejected"  # unchanged


def test_skills_cmd_approve_needs_a_skills_dir(tmp_path, capsys):
    import argparse

    from iris.cli import skills_cmd
    from iris.config import Config
    from iris.skills import SkillProposalStore

    pfile = tmp_path / "p.json"
    content = "---\nname: s\ndescription: d\n---\nbody"
    SkillProposalStore(pfile).add("s", content, "r", kind="new", now=1.0)
    config = Config(skills_dir="", skill_proposals_file=str(pfile))
    rc = skills_cmd(config, argparse.Namespace(skills_action="approve", proposal_id=1))
    assert rc == 2
    assert "IRIS_SKILLS_DIR" in capsys.readouterr().out


def test_doctor_reports_goals_when_enabled(tmp_path, capsys):
    from iris.cli import doctor
    from iris.config import Config
    from iris.goals import GoalStore

    GoalStore(tmp_path / "g.json").add("a standing goal", now=1.0)
    fake = _fake_claude(tmp_path)
    doctor(Config(claude_bin=fake, goals_enabled=True,
                  goals_file=str(tmp_path / "g.json")), probe=False)
    out = capsys.readouterr().out
    assert "goal" in out.lower()


def test_goal_tick_disabled_makes_no_model_call(tmp_path, monkeypatch, capsys):
    # With IRIS_GOALS off the tick short-circuits before any usage fetch or model
    # call, so it's safe to run through the real parser with no stubs.
    from iris.cli import main

    monkeypatch.chdir(tmp_path)
    for key in list(__import__("os").environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    rc = main(["goal-tick"])
    assert rc == 0
    assert "disabled" in capsys.readouterr().out


def test_goals_cmd_list_and_cancel(tmp_path, capsys):
    import argparse

    from iris.cli import goals_cmd
    from iris.config import Config
    from iris.goals import GoalStore

    config = Config(goals_file=str(tmp_path / "g.json"))
    store = GoalStore(tmp_path / "g.json")
    store.add("a standing goal", now=1.0)

    rc = goals_cmd(config, argparse.Namespace(goals_action="list"))
    out = capsys.readouterr().out
    assert rc == 0 and "a standing goal" in out and "#1" in out

    rc = goals_cmd(config, argparse.Namespace(goals_action="cancel", goal_id=1))
    assert rc == 0
    assert GoalStore(tmp_path / "g.json").get(1)["status"] == "cancelled"

    rc = goals_cmd(config, argparse.Namespace(goals_action="cancel", goal_id=99))
    assert rc == 1  # no such goal


def test_doctor_warns_when_browser_grant_lacks_npx(tmp_path, capsys, monkeypatch):
    import iris.cli as cli_mod
    from iris.config import Config

    fake = _fake_claude(tmp_path)
    real_which = cli_mod.shutil.which
    monkeypatch.setattr(cli_mod.shutil, "which",
                        lambda name: None if name == "npx" else real_which(name))
    cli_mod.doctor(Config(claude_bin=fake, jobs_enabled=True, job_grants=["browser"]),
                   probe=False)
    out = capsys.readouterr().out
    assert "browser" in out and "npx" in out


def test_doctor_warns_when_a_workspace_contains_the_state_files(tmp_path, capsys):
    from iris.cli import doctor
    from iris.config import Config
    from iris.workspaces import WorkspaceStore

    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    ws = WorkspaceStore(str(tmp_path / "ws.json"))
    ws.add("everything", str(tmp_path))  # ancestor of the agent state dir
    doctor(Config(claude_bin=_fake_claude(tmp_path), jobs_enabled=True,
                  workspaces_file=str(tmp_path / "ws.json"),
                  schedules_file=str(agent_dir / "iris-schedules.json")),
           probe=False)
    out = capsys.readouterr().out
    assert "everything" in out and "state" in out


def test_schedule_add_through_the_real_parser_creates_a_script_rule(tmp_path, monkeypatch):
    # Regression: the --command option must not collide with the subparser's
    # own dest="command", or `iris schedule add --command ...` falls through to
    # the Discord runner instead of recording the rule. Goes through main() so
    # the real argparse path (not a hand-built Namespace) is exercised.
    from iris.cli import main
    from iris.schedules import ScheduleStore

    monkeypatch.chdir(tmp_path)
    for key in list(__import__("os").environ):
        if key.startswith("IRIS_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("IRIS_SCHEDULES_FILE", str(tmp_path / "s.json"))
    rc = main(["schedule", "add", "--title", "backup", "--at", "+1h",
               "--every", "every 1d", "--command", "echo hi"])
    assert rc == 0
    rules = ScheduleStore(tmp_path / "s.json").all()
    assert len(rules) == 1
    assert rules[0]["command"] == "echo hi"
    assert rules[0]["instructions"] == ""


def test_reminders_tick_runs_subticks_without_a_discord_token(tmp_path, monkeypatch, capsys):
    # The schedules/budget/wakes ticks must not be gated behind the reminder
    # delivery token: a tokenless run should still fire scheduled work.
    from iris.cli import reminders_tick
    from iris.config import Config
    from iris.schedules import ScheduleStore, add_rule

    monkeypatch.chdir(tmp_path)
    sfile = tmp_path / "s.json"
    add_rule(ScheduleStore(sfile), title="b", when="2020-01-01T00:00:00Z",
             every="every 1d", command="true")
    calls = []
    monkeypatch.setattr("iris.schedules.subprocess.Popen",
                        lambda *a, **k: calls.append(a) or type("P", (), {"pid": 4242})())
    rc = reminders_tick(Config(discord_token="", scheduled_jobs_enabled=True,
                               jobs_enabled=True, schedules_file=str(sfile),
                               usage_file=str(tmp_path / "u.json"),
                               wakes_file=str(tmp_path / "w.json"),
                               wakes_state=str(tmp_path / "w.s.json")))
    out = capsys.readouterr().out
    assert rc == 0
    assert "schedules: 1 due, 1 launched" in out
    assert calls  # the script rule actually spawned despite no token
