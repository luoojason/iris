"""Test the Textual TUI with a fake agent. Skipped where textual is absent."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from iris.driver import ClaudeResult
from iris.tui import build_app, inspector_rows, render_sidebar


def test_inspector_rows_lists_active_jobs_and_goals(tmp_path):
    import os

    from iris.config import Config
    from iris.goals import GoalStore
    from iris.jobs import JobStore

    cfg = Config(jobs_enabled=True, goals_enabled=True,
                 jobs_file=str(tmp_path / "jobs.json"),
                 goals_file=str(tmp_path / "goals.json"),
                 usage_file=str(tmp_path / "usage.json"))
    js = JobStore(cfg.jobs_file)
    js.add("upload shorts", "do it", ["subagents"], "", "h", state="running")
    js.update(1, pid=os.getpid())  # live pid so repair leaves it active
    GoalStore(cfg.goals_file).add("ship the roadmap", now=1.0)

    rows = inspector_rows(cfg)
    seen = {(r["kind"], r["id"]) for r in rows}
    assert ("job", 1) in seen and ("goal", 1) in seen


def test_render_sidebar_shows_live_state(tmp_path):
    from iris.config import Config
    from iris.goals import GoalStore
    from iris.jobs import JobStore

    cfg = Config(jobs_enabled=True, goals_enabled=True,
                 jobs_file=str(tmp_path / "jobs.json"),
                 goals_file=str(tmp_path / "goals.json"),
                 usage_file=str(tmp_path / "usage.json"))
    import os
    js = JobStore(cfg.jobs_file)
    js.add("upload shorts", "do it", ["subagents"], "", "h", state="running")
    js.update(1, pid=os.getpid())  # a live pid so repair_dead_runners leaves it active
    GoalStore(cfg.goals_file).add("ship the roadmap", now=1.0)

    out = render_sidebar(cfg, now=1.0)
    assert "JOBS" in out and "#1" in out and "running" in out
    assert "GOALS" in out and "ship the roadmap"[:10] in out
    assert "USAGE" in out


def test_render_sidebar_tolerates_empty_and_missing(tmp_path):
    from iris.config import Config

    # nothing enabled, no state files: must not raise, just show empty/off sections
    out = render_sidebar(Config(usage_file=str(tmp_path / "u.json")), now=1.0)
    assert "JOBS" in out and "GOALS" in out and "USAGE" in out


class FakeAgent:
    def __init__(self):
        self.calls = []
        self.reset_calls = []

    def respond(self, conversation_id, text):
        self.calls.append((conversation_id, text))
        return ClaudeResult(text=f"echo: {text}", session_id="s1", is_error=False)

    def reset(self, conversation_id):
        self.reset_calls.append(conversation_id)
        return True


async def test_submitting_a_message_calls_the_agent():
    agent = FakeAgent()
    app = build_app(agent)()
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "hello there"
        await pilot.press("enter")
        await app.workers.wait_for_complete()
        await pilot.pause()
    assert agent.calls == [("tui:local", "hello there")]


async def test_reset_command_resets_the_conversation():
    agent = FakeAgent()
    app = build_app(agent)()
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "/reset"
        await pilot.press("enter")
        await pilot.pause()
    assert agent.reset_calls == ["tui:local"]
    assert agent.calls == []  # /reset is not sent to the model


async def test_blank_input_does_nothing():
    agent = FakeAgent()
    app = build_app(agent)()
    async with app.run_test() as pilot:
        app.query_one("#prompt").value = "   "
        await pilot.press("enter")
        await pilot.pause()
    assert agent.calls == []


async def test_inspector_opens_and_closes(tmp_path):
    import os

    from iris.config import Config
    from iris.jobs import JobStore

    cfg = Config(jobs_enabled=True, goals_enabled=True,
                 jobs_file=str(tmp_path / "jobs.json"),
                 goals_file=str(tmp_path / "goals.json"),
                 usage_file=str(tmp_path / "usage.json"))
    js = JobStore(cfg.jobs_file)
    js.add("upload", "i", ["subagents"], "", "h", state="running")
    js.update(1, pid=os.getpid())

    app = build_app(FakeAgent(), cfg)()
    async with app.run_test() as pilot:
        await pilot.press("ctrl+o")
        await pilot.pause()
        assert len(app.screen_stack) == 2  # inspector pushed
        assert app.screen.__class__.__name__ == "Inspector"
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1  # back to the chat
