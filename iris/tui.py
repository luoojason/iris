"""A full-screen terminal UI for Iris, built on Textual.

A scrolling conversation on the left with a live "thinking" indicator and a
proper input line, and a right sidebar that shows Iris's live state at a glance
(active jobs, goals and their progress, this month's usage, pending reminders) so
you never have to leave the chat to see what she's doing. The sidebar refreshes
on a timer and only reads the state files: no model call, no behavior of its own.

    python -m iris tui      (needs: pip install 'iris-agent[tui]')
"""

from __future__ import annotations

from typing import Optional

from .agent import Agent
from .config import Config
from .driver import ClaudeError, ClaudeResult

SIDEBAR_REFRESH_SECS = 5.0
_ACTIVE_JOB_STATES = ("pending", "running", "needs_input", "waiting", "parked")


def _jobs_lines(config: Config) -> list[str]:
    lines = ["[b]JOBS[/b]"]
    if not getattr(config, "jobs_enabled", False):
        lines.append("  [dim]off[/dim]")
        return lines
    try:
        from .jobs import JobStore, repair_dead_runners
        store = JobStore(config.jobs_file, keep=config.jobs_keep)
        try:
            repair_dead_runners(store)
        except Exception:
            pass
        active = [j for j in store.all() if j.get("state") in _ACTIVE_JOB_STATES]
    except Exception:
        return ["[b]JOBS[/b]", "  [dim]unavailable[/dim]"]
    lines[0] = f"[b]JOBS[/b]  [dim]{len(active)} active[/dim]"
    if not active:
        lines.append("  [dim]none[/dim]")
    for job in active[:6]:
        lines.append(f"  #{job.get('id')} [cyan]{job.get('state')}[/cyan] "
                     f"{(job.get('title') or '')[:18]}")
    return lines


def _goals_lines(config: Config) -> list[str]:
    lines = ["[b]GOALS[/b]"]
    try:
        from .goals import GoalStore
        active = GoalStore(config.goals_file).active()
    except Exception:
        return ["[b]GOALS[/b]", "  [dim]unavailable[/dim]"]
    suffix = "" if getattr(config, "goals_enabled", False) else " [dim](loop off)[/dim]"
    lines[0] = f"[b]GOALS[/b]  [dim]{len(active)} active[/dim]{suffix}"
    if not active:
        lines.append("  [dim]none[/dim]")
    for goal in active[:5]:
        steps, mx = goal.get("steps", 0), goal.get("max_steps", "?")
        lines.append(f"  #{goal.get('id')} [green]{steps}/{mx}[/green] "
                     f"{(goal.get('text') or '')[:18]}")
    return lines


def _usage_lines(config: Config, now: Optional[float]) -> list[str]:
    lines = ["[b]USAGE[/b]"]
    try:
        from .usage import UsageLedger, percent_used
        entry = UsageLedger(config.usage_file).month(now)
        cost = float(entry.get("cost_usd", 0.0))
        if config.usage_budget_usd > 0:
            pct = percent_used(entry, config.usage_budget_usd)
            lines.append(f"  mo ${cost:.2f}/${config.usage_budget_usd:.0f} "
                         f"[dim]({pct:.0f}%)[/dim]")
        else:
            lines.append(f"  mo ${cost:.2f} [dim](no budget)[/dim]")
    except Exception:
        lines.append("  [dim]unavailable[/dim]")
    # Weekly plan utilization from the cached file only (no network here).
    try:
        import json
        from pathlib import Path
        cache = Path(getattr(config, "proactive_usage_cache", "") or "")
        if cache.exists():
            util = json.loads(cache.read_text("utf-8")).get("utilization")
            if util is not None:
                lines.append(f"  wk [dim]{float(util):.0f}% of plan[/dim]")
    except Exception:
        pass
    return lines


def _reminders_lines(config: Config) -> list[str]:
    try:
        import os
        from .reminders import ReminderStore
        path = os.environ.get("IRIS_REMINDERS_FILE", "iris-reminders.json")
        pending = len(ReminderStore(path).all())
    except Exception:
        return []
    if not pending:
        return []
    return [f"[b]REMINDERS[/b]  [dim]{pending} pending[/dim]"]


def render_sidebar(config: Config, now: Optional[float] = None) -> str:
    """Render the live-state sidebar as Rich markup. Pure: reads state files only,
    never the model, and degrades each section independently so one bad read can
    never blank the whole panel."""
    sections = [
        _jobs_lines(config),
        _goals_lines(config),
        _usage_lines(config, now),
        _reminders_lines(config),
    ]
    blocks = ["\n".join(s) for s in sections if s]
    return "\n\n".join(blocks)


def inspector_rows(config: Config) -> list[dict]:
    """The actionable items for the drill-in inspector: active jobs and goals.

    Pure (reads state files only). Each row is ``{kind, id, state, label}`` where
    kind is 'job' or 'goal'. Jobs first, then goals.
    """
    rows: list[dict] = []
    if getattr(config, "jobs_enabled", False):
        try:
            from .jobs import JobStore, repair_dead_runners
            store = JobStore(config.jobs_file, keep=config.jobs_keep)
            try:
                repair_dead_runners(store)
            except Exception:
                pass
            for j in store.all():
                if j.get("state") in _ACTIVE_JOB_STATES:
                    rows.append({"kind": "job", "id": j.get("id"),
                                 "state": j.get("state", ""), "label": (j.get("title") or "")[:40]})
        except Exception:
            pass
    try:
        from .goals import GoalStore
        for g in GoalStore(config.goals_file).active():
            rows.append({"kind": "goal", "id": g.get("id"),
                         "state": f"{g.get('steps', 0)}/{g.get('max_steps', '?')}",
                         "label": (g.get("text") or "")[:40]})
    except Exception:
        pass
    return rows


def _goal_detail(config: Config, goal_id: int) -> str:
    from .goals import GoalStore
    goal = GoalStore(config.goals_file).get(goal_id)
    if not goal:
        return f"goal #{goal_id}: gone"
    lines = [f"goal #{goal['id']} [{goal.get('status')}]  {goal.get('steps', 0)}/{goal.get('max_steps', '?')} steps",
             goal.get("text", "")]
    for entry in (goal.get("log") or [])[-3:]:
        lines.append(f"  - [{entry.get('status')}] {(entry.get('summary') or entry.get('step') or '')[:80]}")
    return "\n".join(lines)


def build_app(agent: Agent, config: Optional[Config] = None):
    """Build the Textual app class bound to an agent (and optional config for the
    live sidebar). Importing textual lazily so the package imports without it."""
    from textual import work
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, Vertical
    from textual.screen import ModalScreen
    from textual.widgets import DataTable, Footer, Header, Input, LoadingIndicator, RichLog, Static
    from rich.markdown import Markdown
    from rich.text import Text

    class Inspector(ModalScreen):
        """Drill-in over the active jobs and goals: select one, see its detail, and
        act on it (cancel, resume, answer a paused job). Reuses the same shared
        functions the CLI/MCP use, so it adds no behavior and makes no model call."""

        CSS = """
        Inspector { align: center middle; }
        #panel { width: 84%; height: 84%; border: round $primary; background: $surface; padding: 0 1; }
        #itable { height: 1fr; }
        #idetail { height: auto; max-height: 45%; padding: 0 1; color: $text; }
        #ianswer { display: none; border: round $accent; }
        #ianswer.show { display: block; }
        #istatus { dock: bottom; height: 1; color: $accent; }
        """
        BINDINGS = [
            ("escape", "close", "Close"),
            ("enter", "detail", "Detail"),
            ("c", "cancel_item", "Cancel"),
            ("s", "resume_item", "Resume"),
            ("a", "answer_item", "Answer"),
        ]

        def __init__(self, cfg: Config) -> None:
            super().__init__()
            self.cfg = cfg
            self._rows: list[dict] = []

        def compose(self) -> ComposeResult:
            with Vertical(id="panel"):
                yield DataTable(id="itable")
                yield Static("", id="idetail")
                yield Input(placeholder="Answer for the paused job…  (Enter sends)", id="ianswer")
                yield Static("[dim]enter: detail · c: cancel · s: resume · a: answer · esc: close[/dim]",
                             id="istatus")

        def on_mount(self) -> None:
            table = self.query_one("#itable", DataTable)
            table.cursor_type = "row"
            table.add_columns("Kind", "ID", "State", "What")
            self._reload()
            table.focus()

        def _reload(self) -> None:
            table = self.query_one("#itable", DataTable)
            table.clear()
            self._rows = inspector_rows(self.cfg)
            for r in self._rows:
                table.add_row(r["kind"], f"#{r['id']}", r["state"], r["label"])

        def _selected(self) -> Optional[dict]:
            if not self._rows:
                return None
            row = self.query_one("#itable", DataTable).cursor_row
            if row is None or row < 0 or row >= len(self._rows):
                return None
            return self._rows[row]

        def _status(self, text: str) -> None:
            self.query_one("#istatus", Static).update(text)

        def action_close(self) -> None:
            self.dismiss()

        def action_detail(self) -> None:
            item = self._selected()
            if not item:
                return
            if item["kind"] == "job":
                from .jobs import JobStore
                from .jobs_console import format_detail
                job = JobStore(self.cfg.jobs_file, keep=self.cfg.jobs_keep).get(item["id"])
                self.query_one("#idetail", Static).update(format_detail(job) if job else "(gone)")
            else:
                self.query_one("#idetail", Static).update(_goal_detail(self.cfg, item["id"]))

        def action_cancel_item(self) -> None:
            item = self._selected()
            if not item:
                return
            import time
            if item["kind"] == "job":
                from .jobs import JobStore, cancel
                self._status(cancel(JobStore(self.cfg.jobs_file, keep=self.cfg.jobs_keep), item["id"]))
            else:
                from .goals import GoalStore
                GoalStore(self.cfg.goals_file).transition(item["id"], "cancelled", time.time())
                self._status(f"cancelled goal #{item['id']}")
            self._reload()

        def action_resume_item(self) -> None:
            item = self._selected()
            if not item or item["kind"] != "job":
                self._status("resume applies to a job")
                return
            from .jobs import JobStore, resume_job, spawn_runner
            store = JobStore(self.cfg.jobs_file, keep=self.cfg.jobs_keep)
            self._status(resume_job(store, item["id"], spawn=spawn_runner))
            self._reload()

        def action_answer_item(self) -> None:
            item = self._selected()
            if not item or item["kind"] != "job":
                return
            box = self.query_one("#ianswer", Input)
            box.add_class("show")
            box.focus()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            if event.input.id != "ianswer":
                return
            event.stop()  # don't let the answer bubble up as a chat prompt too
            item = self._selected()
            value = event.value.strip()
            event.input.value = ""
            event.input.remove_class("show")
            if item and item["kind"] == "job" and value:
                from .jobs import JobStore, resume_job, spawn_runner
                store = JobStore(self.cfg.jobs_file, keep=self.cfg.jobs_keep)
                self._status(resume_job(store, item["id"], answer=value, spawn=spawn_runner))
                self._reload()
            self.query_one("#itable", DataTable).focus()

    class IrisApp(App):
        CSS = """
        #body { height: 1fr; }
        #log { width: 2fr; padding: 0 1; background: $surface; }
        #sidebar { width: 34; padding: 0 1; border-left: solid $primary; overflow-y: auto; }
        #thinking { display: none; height: 1; color: $accent; }
        #thinking.busy { display: block; }
        Input { dock: bottom; border: round $primary; }
        """
        TITLE = "Iris"
        BINDINGS = [
            ("ctrl+c", "quit", "Quit"),
            ("ctrl+q", "quit", "Quit"),
            ("ctrl+n", "reset", "New chat"),
            ("ctrl+r", "refresh", "Refresh"),
            ("ctrl+o", "inspect", "Inspect"),
            ("tab", "inspect", "Inspect"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.agent = agent
            self.config = config
            self.conversation_id = "tui:local"
            self.busy = False

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with Horizontal(id="body"):
                yield RichLog(id="log", wrap=True, markup=True, highlight=False)
                yield Static("", id="sidebar")
            yield LoadingIndicator(id="thinking")
            yield Input(placeholder="Message Iris…   (/reset, /quit)", id="prompt")
            yield Footer()

        def on_mount(self) -> None:
            self.query_one("#log", RichLog).write(
                Text("Iris is ready. Ctrl+N new chat, Ctrl+R refresh, Ctrl+C quit.", style="dim")
            )
            if self.config is not None:
                self.refresh_sidebar()
                self.set_interval(SIDEBAR_REFRESH_SECS, self.refresh_sidebar)
            else:
                self.query_one("#sidebar", Static).update("[dim](no config)[/dim]")
            self.query_one("#prompt", Input).focus()

        def refresh_sidebar(self) -> None:
            try:
                self.query_one("#sidebar", Static).update(render_sidebar(self.config))
            except Exception:
                pass  # a sidebar hiccup must never take down the chat

        def on_input_submitted(self, event: Input.Submitted) -> None:
            text = event.value.strip()
            self.query_one("#prompt", Input).value = ""
            if not text or self.busy:
                return
            if text in ("/quit", "/exit"):
                self.exit()
                return
            if text == "/reset":
                self.action_reset()
                return
            self.query_one("#log", RichLog).write(Text(f"you   {text}", style="bold cyan"))
            self._set_busy(True)
            self._run_turn(text)

        @work(thread=True)
        def _run_turn(self, text: str) -> None:
            result: Optional[ClaudeResult] = None
            error: Optional[str] = None
            try:
                result = self.agent.respond(self.conversation_id, text)
            except ClaudeError as exc:
                error = str(exc)
            except Exception as exc:  # never leave the input disabled on a crash
                error = f"unexpected error: {exc}"
            self.call_from_thread(self._show, result, error)

        def _show(self, result: Optional[ClaudeResult], error: Optional[str]) -> None:
            self._set_busy(False)
            log = self.query_one("#log", RichLog)
            if error:
                log.write(Text(f"iris  (unavailable) {error}", style="red"))
                return
            if result.is_error:
                log.write(Text(f"iris  (error) {result.error}", style="red"))
                return
            log.write(Text("iris", style="bold magenta"))
            log.write(Markdown(result.text.strip() or "(no response)"))
            if self.config is not None:
                self.refresh_sidebar()  # a turn may have started a job / advanced a goal

        def _set_busy(self, busy: bool) -> None:
            self.busy = busy
            self.sub_title = "thinking…" if busy else ""
            self.query_one("#thinking", LoadingIndicator).set_class(busy, "busy")
            prompt = self.query_one("#prompt", Input)
            prompt.disabled = busy
            if not busy:
                prompt.focus()

        def action_reset(self) -> None:
            self.agent.reset(self.conversation_id)
            self.query_one("#log", RichLog).write(Text("— new conversation —", style="dim italic"))

        def action_refresh(self) -> None:
            self.refresh_sidebar()

        def action_inspect(self) -> None:
            # Open the drill-in over jobs/goals. Guard against re-entry so the
            # binding firing while the modal is already up can't stack inspectors.
            if self.config is None or len(self.screen_stack) > 1:
                return
            self.push_screen(Inspector(self.config))

    return IrisApp


def run(config: Optional[Config] = None) -> None:
    config = config or Config.from_env()
    try:
        import textual  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "The TUI needs textual. Install it with:\n"
            "    pip install 'iris-agent[tui]'\n"
            "or just use the plain REPL: python -m iris chat"
        ) from exc
    agent = Agent.from_config(config)
    build_app(agent, config)().run()
