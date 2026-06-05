"""A small full-screen terminal UI for Iris, built on Textual.

A scrolling conversation, a live "thinking" indicator while the brain works, and
a proper input line with history and editing. Same Agent core as every other
front end; this is just a nicer way to talk to it than the bare REPL.

    python -m iris tui      (needs: pip install 'iris-agent[tui]')
"""

from __future__ import annotations

from typing import Optional

from .agent import Agent
from .config import Config
from .driver import ClaudeError, ClaudeResult


def build_app(agent: Agent):
    """Build the Textual app class bound to an agent. Importing textual lazily."""
    from textual import work
    from textual.app import App, ComposeResult
    from textual.widgets import Footer, Header, Input, LoadingIndicator, RichLog
    from rich.markdown import Markdown
    from rich.text import Text

    class IrisApp(App):
        CSS = """
        Screen { layers: base; }
        #log { height: 1fr; padding: 0 1; background: $surface; }
        #thinking { display: none; height: 1; color: $accent; }
        #thinking.busy { display: block; }
        Input { dock: bottom; border: round $primary; }
        """
        TITLE = "Iris"
        BINDINGS = [
            ("ctrl+c", "quit", "Quit"),
            ("ctrl+r", "reset", "New chat"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.agent = agent
            self.conversation_id = "tui:local"
            self.busy = False

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield RichLog(id="log", wrap=True, markup=True, highlight=False)
            yield LoadingIndicator(id="thinking")
            yield Input(placeholder="Message Iris…   (/reset, /quit)", id="prompt")
            yield Footer()

        def on_mount(self) -> None:
            self.query_one("#log", RichLog).write(
                Text("Iris is ready. Type a message. Ctrl+R for a new chat, Ctrl+C to quit.", style="dim")
            )
            self.query_one("#prompt", Input).focus()

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
            try:
                result: Optional[ClaudeResult] = self.agent.respond(self.conversation_id, text)
                error = None
            except ClaudeError as exc:
                result, error = None, str(exc)
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
    build_app(agent)().run()
