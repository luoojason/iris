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


def test_doctor_reports_missing_binary():
    from iris.cli import doctor
    from iris.config import Config

    # A bogus binary name: doctor must fail cleanly without any network call.
    rc = doctor(Config(claude_bin="iris-no-such-claude-binary"), probe=False)
    assert rc == 1
