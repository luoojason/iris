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


def build_app(agent: Agent, config: Optional[Config] = None):
    """Build the Textual app class bound to an agent (and optional config for the
    live sidebar). Importing textual lazily so the package imports without it."""
    from textual import work
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal
    from textual.widgets import Footer, Header, Input, LoadingIndicator, RichLog, Static
    from rich.markdown import Markdown
    from rich.text import Text

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
