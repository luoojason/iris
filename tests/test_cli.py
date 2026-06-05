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
    for command in ("discord", "telegram", "chat", "doctor"):
        assert command in result.stdout
