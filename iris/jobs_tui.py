"""A full-screen terminal view of the background jobs, built on Textual.

`iris jobs --tui` shows the job registry as a live table and lets the owner
act on the selected job — cancel, resume, re-run — with single keypresses. It
is a thin view: every action routes through the same shared functions the
`iris jobs` CLI uses (`iris/jobs.py`, `iris/jobs_console.py`), so the TUI adds
no behavior of its own and makes no model call.

    python -m iris jobs --tui      (needs: pip install 'iris-agent[tui]')
"""

from __future__ import annotations

from typing import Optional

from .config import Config
from .jobs import (
    JobStore,
    cancel,
    repair_dead_runners,
    rerun_job,
    spawn_runner,
)
from .jobs_console import _age, format_detail


def build_jobs_app(config: Config, *, spawn=None):
    """Build the Textual jobs app bound to a config. Importing textual lazily."""
    from textual.app import App, ComposeResult
    from textual.widgets import DataTable, Footer, Header, Static

    launch = spawn or spawn_runner

    class JobsApp(App):
        CSS = """
        #jobs { height: 1fr; }
        #detail { height: auto; max-height: 40%; padding: 0 1; color: $text; background: $surface; }
        #status { dock: bottom; height: 1; color: $accent; }
        """
        TITLE = "Iris jobs"
        BINDINGS = [
            ("q", "quit", "Quit"),
            ("r", "refresh", "Refresh"),
            ("enter", "detail", "Detail"),
            ("c", "cancel_job", "Cancel"),
            ("s", "resume_job", "Resume"),
            ("e", "rerun_job", "Re-run"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.store = JobStore(config.jobs_file, keep=config.jobs_keep)
            self._row_ids: list[int] = []
            self.detail_text = ""  # mirrors the detail panel (testability seam)

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield DataTable(id="jobs")
            yield Static("", id="detail")
            yield Static("", id="status")
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one("#jobs", DataTable)
            table.cursor_type = "row"
            table.add_columns("ID", "State", "Title", "When", "Grants / WS")
            self.refresh_jobs()

        def refresh_jobs(self) -> None:
            repair_dead_runners(self.store)
            table = self.query_one("#jobs", DataTable)
            table.clear()
            self._row_ids = []
            for job in sorted(self.store.all(), key=lambda j: j.get("id", 0), reverse=True):
                grants = ",".join(job.get("grants") or []) or "-"
                ws = job.get("workspace") or "-"
                table.add_row(
                    f"#{job.get('id')}", job.get("state", ""),
                    (job.get("title") or "")[:30], _age(job), f"{grants} / {ws}",
                )
                self._row_ids.append(job.get("id"))

        def selected_id(self) -> Optional[int]:
            if not self._row_ids:
                return None
            row = self.query_one("#jobs", DataTable).cursor_row
            if row is None or row < 0 or row >= len(self._row_ids):
                return None
            return self._row_ids[row]

        def _status(self, text: str) -> None:
            self.query_one("#status", Static).update(text)

        def action_refresh(self) -> None:
            self.refresh_jobs()
            self._status("refreshed")

        def action_detail(self) -> None:
            jid = self.selected_id()
            if jid is None:
                return
            job = self.store.get(jid)
            self.detail_text = format_detail(job) if job else ""
            self.query_one("#detail", Static).update(self.detail_text)

        def action_cancel_job(self) -> None:
            jid = self.selected_id()
            if jid is None:
                return
            self._status(cancel(self.store, jid))
            self.refresh_jobs()

        def action_resume_job(self) -> None:
            jid = self.selected_id()
            if jid is None:
                return
            job = self.store.get(jid)
            if not job or job["state"] not in ("pending", "parked"):
                self._status(f"job #{jid} cannot be resumed (state: {job['state'] if job else 'gone'})")
                return
            self.store.transition(jid, ("parked",), "pending")
            launch(jid, store=self.store)
            self._status(f"resumed job #{jid}")
            self.refresh_jobs()

        def action_rerun_job(self) -> None:
            jid = self.selected_id()
            if jid is None:
                return
            clone = rerun_job(self.store, jid, config.home_channel)
            if clone is None:
                return
            launch(clone["id"], store=self.store)
            self._status(f"re-ran job #{jid} as #{clone['id']}")
            self.refresh_jobs()

    return JobsApp


def run(config: Optional[Config] = None) -> int:
    config = config or Config.from_env()
    try:
        import textual  # noqa: F401
    except ImportError as exc:
        raise SystemExit(
            "The jobs TUI needs textual. Install it with:\n"
            "    pip install 'iris-agent[tui]'\n"
            "or use the plain table: python -m iris jobs"
        ) from exc
    build_jobs_app(config)().run()
    return 0
