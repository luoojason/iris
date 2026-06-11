"""Smoke tests for the jobs TUI. Skipped where textual is absent."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

from iris.config import Config
from iris.jobs import JobStore
from iris.jobs_tui import build_jobs_app


def cfg(tmp_path):
    return Config(
        jobs_enabled=True,
        jobs_file=str(tmp_path / "jobs.json"),
        workspaces_file=str(tmp_path / "ws.json"),
        home_channel="home-1",
    )


async def test_tui_lists_jobs(tmp_path):
    config = cfg(tmp_path)
    store = JobStore(config.jobs_file)
    store.add("first", "x", ["subagents"], "", "home-1")
    store.add("second", "y", ["subagents"], "repo", "home-1")
    app = build_jobs_app(config)()
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable
        table = app.query_one("#jobs", DataTable)
        assert table.row_count == 2
        # newest first
        assert app._row_ids == [2, 1]


async def test_tui_cancel_action_cancels_selected(tmp_path):
    config = cfg(tmp_path)
    store = JobStore(config.jobs_file)
    store.add("a", "x", [], "", "home-1")
    store.add("b", "y", [], "", "home-1")
    app = build_jobs_app(config)()
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable
        app.query_one("#jobs", DataTable).move_cursor(row=0)  # newest = #2
        app.action_cancel_job()
        await pilot.pause()
    assert JobStore(config.jobs_file).get(2)["state"] == "cancelled"
    assert JobStore(config.jobs_file).get(1)["state"] == "pending"


async def test_tui_rerun_action_spawns_clone(tmp_path):
    config = cfg(tmp_path)
    store = JobStore(config.jobs_file)
    store.add("audit", "look", ["subagents"], "", "home-1")
    store.transition(1, ("pending",), "done", report="old")
    spawned = []
    app = build_jobs_app(config, spawn=lambda jid, **k: spawned.append(jid))()
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable
        app.query_one("#jobs", DataTable).move_cursor(row=0)
        app.action_rerun_job()
        await pilot.pause()
    assert spawned == [2]
    assert JobStore(config.jobs_file).get(2)["instructions"] == "look"


async def test_tui_detail_renders_selected(tmp_path):
    config = cfg(tmp_path)
    JobStore(config.jobs_file).add("audit", "the instructions", ["subagents"], "", "home-1")
    app = build_jobs_app(config)()
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable
        app.query_one("#jobs", DataTable).move_cursor(row=0)
        app.action_detail()
        await pilot.pause()
        assert "the instructions" in app.detail_text
