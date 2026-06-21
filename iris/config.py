"""Configuration, loaded from the environment (optionally seeded by a .env).

Kept to plain environment variables so the agent is easy to run anywhere a
shell can reach the ``claude`` binary: a laptop, a VPS, a systemd unit.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def load_dotenv(path: str | os.PathLike[str] = ".env") -> None:
    """Minimal .env reader: KEY=VALUE lines, ``#`` comments, no interpolation.

    Existing environment variables always win, so real env beats the file.
    """
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _split(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _flag(value: Optional[str], default: bool) -> bool:
    """Parse a boolean env var, falling back to ``default`` when it is unset."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    discord_token: str = ""
    telegram_token: str = ""
    # Restrict who the bot answers. Empty means "anyone in channels it sees".
    allowed_user_ids: list[str] = field(default_factory=list)
    # Only respond in these channel ids (empty = respond anywhere it is allowed).
    allowed_channel_ids: list[str] = field(default_factory=list)
    # Respond to every message in allowed channels, not just @mentions.
    respond_without_mention: bool = False
    # Start a thread when a task is begun in a regular channel, so the general
    # channel stays a clean launcher and each task gets its own focused space.
    auto_thread: bool = False

    claude_bin: str = "claude"
    model: Optional[str] = None
    # Optional lighter model for trivial turns (enables per-turn routing when set).
    light_model: str = ""
    # A message at or under this many characters can be routed to the light model
    # (if it also clears the other trivial checks). Raise to route more aggressively.
    trivial_max_chars: int = 140
    persona_file: Optional[str] = None
    # Owner-edited standing orders (durable rules, not facts) appended to the
    # system prompt every turn. Keep it small: every byte is re-billed per turn.
    standing_orders_file: Optional[str] = None
    connections_file: str = "iris-connections.json"
    mcp_config: Optional[str] = None
    permission_mode: str = "default"
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    # Deny the dangerous built-in tools (Bash, Write, WebFetch, ...) by default so
    # IRIS_ALLOWED_TOOLS is a real boundary, not just an auto-approve list. Turn
    # off only if you want the agent to have host shell/file/web reach.
    restrict_builtin_tools: bool = True
    # Keep Claude Code's native auto-memory off so the MCP memory tool is the store.
    disable_auto_memory: bool = True
    add_dirs: list[str] = field(default_factory=list)
    # Where inbound images/files are downloaded so the brain's Read tool can see them.
    attachments_dir: str = "iris-attachments"
    # A directory of skill folders (each with SKILL.md) to make available to the brain.
    skills_dir: str = ""
    # Staging area for Iris's proposed changes to her own skills. A proposal is
    # never live until the owner runs `iris skills approve <id>`: self-modifying
    # her own behavior is the highest-stakes action, so it is always owner-gated.
    skill_proposals_file: str = "iris-skill-proposals.json"
    # Transcribe inbound voice messages locally (needs the [voice] extra). Off by
    # default: the first voice message downloads a whisper model and runs CPU
    # inference, which can be slow on small hosts.
    voice_enabled: bool = False
    voice_model: str = "base"

    # Owner-registered directories jobs may work in (names, never paths, cross
    # the model boundary). Edited only via `iris workspaces add/remove/list`.
    workspaces_file: str = "iris-workspaces.json"

    # Background jobs (the hybrid job coordinator). Off by default; everything
    # below is inert until IRIS_JOBS is set.
    jobs_enabled: bool = False
    jobs_file: str = "iris-jobs.json"
    # The grants ceiling: the most a job may ever be granted beyond subagents.
    job_grants: list[str] = field(default_factory=list)
    # Active (pending+running) jobs past this count are queued, not launched.
    jobs_max: int = 2
    # Auto-prune terminal jobs (done/failed/cancelled) past this many, keeping
    # the most recent. Active jobs are never pruned.
    jobs_keep: int = 50
    job_timeout: float = 1800.0
    # Optional model/persona for job turns; empty falls back to the chat model.
    job_model: str = ""
    # The model a job uses when the model flags it as genuinely hard (heavy=True):
    # everyday tasks run on the cheaper base model, hard ones escalate to this.
    job_model_heavy: str = "claude-opus-4-8"
    job_persona: str = ""
    # Verification gate: before a finished job reports "done", an independent
    # cheap model rules whether its report actually satisfies the instructions
    # (it can only annotate, never suppress; a failed check flags the report).
    # Off by default. Empty job_verify_model falls back to goal_judge_model.
    job_verify_enabled: bool = False
    job_verify_model: str = ""
    # Pause-and-ask: a job that hits a fork it can't resolve may end a turn with a
    # 'QUESTION:' line; the runner pauses it (state needs_input) and pings instead
    # of guessing. The owner answers via resume_job(answer=...) and it resumes the
    # same session. This caps how many times one job may pause, so it can't loop.
    job_max_questions: int = 5
    # The browser job grant: how to launch the Playwright MCP server, and the
    # isolated profile directory it gets (never the owner's real browser
    # profile). Only used when a job is granted 'browser'.
    browser_mcp_cmd: str = "npx @playwright/mcp@latest --headless"
    browser_profile_dir: str = "iris-browser-profile"
    # Playwright MCP tools a browser job may not call even though the server is
    # allowed. The default denies only in-page code execution (arbitrary JS in
    # an authenticated page); file upload is allowed so the agent can do what a
    # human does. Set IRIS_BROWSER_DENY_TOOLS to retune (empty denies none).
    browser_deny_tools: list[str] = field(
        default_factory=lambda: ["browser_evaluate", "browser_run_code_unsafe"])
    # Where finished background work queues notes for the next chat turn.
    inbox_file: str = "iris-inbox.json"
    # The owner's recorded home channel (job pings, artifact uploads).
    home_channel: str = ""

    # Autonomous resume: a finished background command the owner launched with
    # autoresume=True may, when this is on, fire ONE follow-up turn on the home
    # conversation so a chain can carry itself forward. Off by default; a daily
    # cap bounds runaway chains; the bot poll loop reads the cross-process queue.
    # See iris/autoresume.py.
    auto_resume: bool = False
    auto_resume_max_per_day: int = 12
    resume_queue_file: str = "iris-resume.json"
    resume_state_file: str = "iris-resume.state.json"
    resume_poll_secs: float = 20.0

    # Proactive reviews (iris/proactive.py): assist (find work) and maintain
    # (self-housekeeping) run on a cron. Off by default. Gated on the REAL weekly
    # plan usage (the OAuth usage endpoint): a review runs only while seven-day
    # utilization is under proactive_usage_max, with the credit-guard park as a
    # hard backstop. creds path empty -> ~/.claude/.credentials.json at runtime.
    proactive_enabled: bool = False
    proactive_usage_max: float = 80.0
    proactive_usage_cache: str = "iris-usage-weekly.json"
    proactive_creds_path: str = ""

    # The goal loop (iris/goals.py): a standing objective the clock advances one
    # step at a time until it is done or needs the owner. Off by default; gated on
    # the SAME real-weekly-usage leash as the proactive reviews (proactive_usage_*),
    # so a goal step never crowds out Jason's own work. goals_max_steps is the
    # default per-goal step budget; a goal may set its own. The judge model is a
    # cheap, independent second model that rules on each step's reported progress.
    goals_enabled: bool = False
    goals_file: str = "iris-goals.json"
    goals_max_steps: int = 20
    goal_judge_model: str = "claude-haiku-4-5"
    # When the judge rules a goal "done", an independent verifier turn (read-only)
    # checks the actual work before it's accepted; an unconfirmed/erroring verify
    # asks the owner instead of silently completing. Fires ONLY on a done verdict,
    # so it adds at most one cheap call per goal completion, not per step.
    goals_verify_done: bool = True

    # Scheduled jobs: the one place the clock may start work, and only work
    # the owner pre-recorded verbatim (see iris/schedules.py). Off by default,
    # gated separately from IRIS_JOBS. schedule_monthly_cap is the default
    # per-rule monthly fire cap (a rule can set its own).
    scheduled_jobs_enabled: bool = False
    schedules_file: str = "iris-schedules.json"
    schedule_monthly_cap: int = 62

    # The owner's wiki vault (Obsidian-style). Empty disables the wiki tools.
    wiki_dir: str = ""

    # YouTube channel view-counts tool (channel_views): reads public view counts
    # via yt-dlp, no browser. Empty channel disables it. yt_dlp_bin is the path to
    # yt-dlp (the MCP server runs with a minimal PATH, so a full path is safest).
    youtube_channel_id: str = ""
    yt_dlp_bin: str = "yt-dlp"

    # Event wakes: owner-authored rules the reminders tick evaluates cheaply
    # (no model call, ever). The state file is tick-owned bookkeeping.
    wakes_file: str = "iris-wakes.json"
    wakes_state: str = "iris-wakes.state.json"
    # Per-fetch timeout for url / url_pattern wake kinds (the change watcher).
    wake_http_timeout: float = 15.0

    # Webhook wakes: a small inbound HTTP listener that turns an authorized POST
    # into a wake (a Discord ping + a fold-back inbox note), never a model call.
    # Off by default. Bound to localhost by default; a mandatory shared token is
    # required (the server refuses to run without one). The payload only ever
    # becomes text in a note, never code and never a model prompt.
    webhook_enabled: bool = False
    webhook_bind: str = "127.0.0.1"
    webhook_port: int = 8787
    webhook_token: str = ""
    webhook_channel: str = ""

    # Quiet heartbeat: an owner-authored checklist of "should be true" conditions
    # (disk free, a file is fresh, a URL is up) the reminders tick evaluates with
    # no model call. Silent when healthy; one consolidated ping only when the set
    # of failing checks changes. Inert until the checks file exists.
    heartbeat_file: str = "iris-heartbeat.json"
    heartbeat_state: str = "iris-heartbeat.state.json"
    heartbeat_http_timeout: float = 15.0

    # Credit guard: the usage ledger always records; the budget (USD-estimate
    # per month, 0 = off) turns on threshold pings, job parking at park_at%,
    # and tighter light-model routing at tighten_at%.
    usage_file: str = "iris-usage.json"
    usage_budget_usd: float = 0.0
    usage_tighten_at: float = 80.0
    usage_park_at: float = 95.0
    usage_ping_at: list[float] = field(default_factory=lambda: [50.0, 80.0, 95.0])
    tighten_factor: float = 3.0

    # The memory tool's store, and the byte budget for the pinned-memory digest
    # rendered into the system prompt every turn (0 = no digest). The budget
    # halves while the credit guard is running hot.
    memory_file: str = "iris-memory.json"
    memory_digest_bytes: int = 2400

    # The active-jobs digest: a tier-0 view of background jobs in flight (and just
    # finished), injected into the system prompt every turn so any session (chat,
    # proactive, post-compaction) sees what is already running and never launches a
    # duplicate. Read fresh from the job store each turn. 0 disables it.
    jobs_digest_bytes: int = 600
    jobs_digest_recent_secs: int = 3600

    session_store_path: str = "iris-sessions.json"
    # When set, append one JSON line of telemetry per turn to this file. Opt-in;
    # empty means no metrics are written (the default for the published agent).
    metrics_file: str = ""
    # Trace ledger: append one structured record per claude -p invocation across
    # every path (chat, job, proactive, goal, compaction) to this file. Opt-in.
    # Content (prompt/reply/raw error) is captured only when trace_capture_content
    # is true; by default only metadata + an error category are stored.
    trace_file: str = ""
    trace_capture_content: bool = False
    # Just-in-time approval (Discord Approve/Deny buttons via the native
    # --permission-prompt-tool). Off by default. The approvals MCP server must be
    # wired into mcp.json separately; this only turns the gate on.
    approvals_enabled: bool = False
    approval_timeout: float = 300.0
    approvals_file: str = "iris-approvals.json"

    # Proactive notifications (iris watch). notify_channel is the Discord channel
    # or DM to ping; watch_min_seconds is the success-ping threshold so quick
    # commands stay silent; notify_persona is an optional voice for proactive
    # messages (falls back to persona_file).
    notify_channel: str = ""
    watch_min_seconds: float = 30.0
    notify_persona: Optional[str] = None
    turn_timeout: float = 300.0
    # Transient (rate-limit / overload) retries, with exponential backoff.
    max_retries: int = 2
    retry_base_delay: float = 2.0
    # Timeout retries are separate: a hung turn rarely recovers by waiting another
    # full timeout, so the default is to report it at once rather than block.
    timeout_max_retries: int = 0
    # Let the user redirect a turn mid-flight (stream-json transport) instead of
    # waiting for it to finish. Off by default; the one-shot driver is the safe
    # fallback. See iris/stream_driver.py.
    live_interrupt: bool = False
    # Seconds of silence (no event) before a streaming turn is treated as hung.
    stream_idle_timeout: float = 300.0
    # Hard ceiling on a whole streaming turn, however lively.
    stream_total_timeout: float = 1800.0
    # Seconds a turn may run before the adapter sends a short interim "on it" line,
    # so a slow turn never looks like a hang. Only used by the conversation runner.
    ack_delay: float = 4.0
    # Compact a conversation when a turn's context reaches this many tokens: the
    # accurate trigger, since it catches tool-heavy turns. 0 disables it.
    compact_at_tokens: int = 150000
    # Backstop trigger: also compact after this many turns on one session, in
    # case usage tokens are ever unavailable. 0 disables it.
    compact_every: int = 60
    # Recent (user, reply) pairs carried into a compaction summary. The summary
    # runs on a fresh session seeded with these, off the conversation lock.
    compact_seed_turns: int = 16

    @classmethod
    def from_env(cls, *, dotenv: str | os.PathLike[str] = ".env") -> "Config":
        load_dotenv(dotenv)
        return cls(
            discord_token=os.environ.get("IRIS_DISCORD_TOKEN", ""),
            telegram_token=os.environ.get("IRIS_TELEGRAM_TOKEN", ""),
            allowed_user_ids=_split(os.environ.get("IRIS_ALLOWED_USER_IDS")),
            allowed_channel_ids=_split(os.environ.get("IRIS_ALLOWED_CHANNEL_IDS")),
            respond_without_mention=_flag(os.environ.get("IRIS_RESPOND_WITHOUT_MENTION"), False),
            auto_thread=_flag(os.environ.get("IRIS_AUTO_THREAD"), False),
            claude_bin=os.environ.get("IRIS_CLAUDE_BIN", "claude"),
            model=os.environ.get("IRIS_MODEL") or None,
            light_model=os.environ.get("IRIS_MODEL_LIGHT", ""),
            trivial_max_chars=int(os.environ.get("IRIS_TRIVIAL_MAX_CHARS", "140")),
            persona_file=os.environ.get("IRIS_PERSONA_FILE") or None,
            standing_orders_file=os.environ.get("IRIS_STANDING_ORDERS_FILE") or None,
            connections_file=os.environ.get("IRIS_CONNECTIONS_FILE", "iris-connections.json"),
            mcp_config=os.environ.get("IRIS_MCP_CONFIG") or None,
            permission_mode=os.environ.get("IRIS_PERMISSION_MODE", "default"),
            allowed_tools=_split(os.environ.get("IRIS_ALLOWED_TOOLS")),
            disallowed_tools=_split(os.environ.get("IRIS_DISALLOWED_TOOLS")),
            restrict_builtin_tools=_flag(os.environ.get("IRIS_RESTRICT_BUILTIN_TOOLS"), True),
            disable_auto_memory=_flag(os.environ.get("IRIS_DISABLE_AUTO_MEMORY"), True),
            add_dirs=_split(os.environ.get("IRIS_ADD_DIRS")),
            attachments_dir=os.environ.get("IRIS_ATTACHMENTS_DIR", "iris-attachments"),
            skills_dir=os.environ.get("IRIS_SKILLS_DIR", ""),
            skill_proposals_file=os.environ.get("IRIS_SKILL_PROPOSALS_FILE", "iris-skill-proposals.json"),
            voice_enabled=_flag(os.environ.get("IRIS_VOICE"), False),
            voice_model=os.environ.get("IRIS_VOICE_MODEL", "base"),
            workspaces_file=os.environ.get("IRIS_WORKSPACES_FILE", "iris-workspaces.json"),
            jobs_enabled=_flag(os.environ.get("IRIS_JOBS"), False),
            jobs_file=os.environ.get("IRIS_JOBS_FILE", "iris-jobs.json"),
            job_grants=_split(os.environ.get("IRIS_JOB_GRANTS")),
            jobs_max=int(os.environ.get("IRIS_JOBS_MAX", "2")),
            jobs_keep=int(os.environ.get("IRIS_JOBS_KEEP", "50")),
            job_timeout=float(os.environ.get("IRIS_JOB_TIMEOUT", "1800")),
            job_model=os.environ.get("IRIS_JOB_MODEL", ""),
            job_model_heavy=os.environ.get("IRIS_JOB_MODEL_HEAVY", "claude-opus-4-8"),
            job_persona=os.environ.get("IRIS_JOB_PERSONA", ""),
            job_verify_enabled=_flag(os.environ.get("IRIS_JOB_VERIFY"), False),
            job_verify_model=os.environ.get("IRIS_JOB_VERIFY_MODEL", ""),
            job_max_questions=int(os.environ.get("IRIS_JOB_MAX_QUESTIONS", "5")),
            browser_mcp_cmd=os.environ.get(
                "IRIS_BROWSER_MCP_CMD", "npx @playwright/mcp@latest --headless"),
            browser_profile_dir=os.environ.get(
                "IRIS_BROWSER_PROFILE_DIR", "iris-browser-profile"),
            browser_deny_tools=(
                _split(os.environ["IRIS_BROWSER_DENY_TOOLS"])
                if "IRIS_BROWSER_DENY_TOOLS" in os.environ
                else ["browser_evaluate", "browser_run_code_unsafe"]),
            inbox_file=os.environ.get("IRIS_INBOX_FILE", "iris-inbox.json"),
            home_channel=os.environ.get("IRIS_DISCORD_HOME_CHANNEL", ""),
            auto_resume=_flag(os.environ.get("IRIS_AUTO_RESUME"), False),
            auto_resume_max_per_day=int(os.environ.get("IRIS_AUTO_RESUME_MAX_PER_DAY", "12")),
            resume_queue_file=os.environ.get("IRIS_RESUME_QUEUE_FILE", "iris-resume.json"),
            resume_state_file=os.environ.get("IRIS_RESUME_STATE", "iris-resume.state.json"),
            resume_poll_secs=float(os.environ.get("IRIS_RESUME_POLL_SECS", "20")),
            proactive_enabled=_flag(os.environ.get("IRIS_PROACTIVE"), False),
            proactive_usage_max=float(os.environ.get("IRIS_PROACTIVE_USAGE_MAX", "80")),
            proactive_usage_cache=os.environ.get("IRIS_PROACTIVE_USAGE_CACHE", "iris-usage-weekly.json"),
            proactive_creds_path=os.environ.get("IRIS_PROACTIVE_CREDS", ""),
            goals_enabled=_flag(os.environ.get("IRIS_GOALS"), False),
            goals_file=os.environ.get("IRIS_GOALS_FILE", "iris-goals.json"),
            goals_max_steps=int(os.environ.get("IRIS_GOALS_MAX_STEPS", "20")),
            goal_judge_model=os.environ.get("IRIS_GOAL_JUDGE_MODEL", "claude-haiku-4-5"),
            goals_verify_done=_flag(os.environ.get("IRIS_GOALS_VERIFY_DONE"), True),
            scheduled_jobs_enabled=_flag(os.environ.get("IRIS_SCHEDULED_JOBS"), False),
            schedules_file=os.environ.get("IRIS_SCHEDULES_FILE", "iris-schedules.json"),
            schedule_monthly_cap=int(os.environ.get("IRIS_SCHEDULE_MONTHLY_CAP", "62")),
            wiki_dir=os.environ.get("IRIS_WIKI_DIR", ""),
            youtube_channel_id=os.environ.get("IRIS_YOUTUBE_CHANNEL", ""),
            yt_dlp_bin=os.environ.get("IRIS_YT_DLP_BIN", "yt-dlp"),
            wakes_file=os.environ.get("IRIS_WAKES_FILE", "iris-wakes.json"),
            wakes_state=os.environ.get("IRIS_WAKES_STATE", "iris-wakes.state.json"),
            wake_http_timeout=float(os.environ.get("IRIS_WAKE_HTTP_TIMEOUT", "15")),
            webhook_enabled=_flag(os.environ.get("IRIS_WEBHOOK"), False),
            webhook_bind=os.environ.get("IRIS_WEBHOOK_BIND", "127.0.0.1"),
            webhook_port=int(os.environ.get("IRIS_WEBHOOK_PORT", "8787")),
            webhook_token=os.environ.get("IRIS_WEBHOOK_TOKEN", ""),
            webhook_channel=os.environ.get("IRIS_WEBHOOK_CHANNEL", ""),
            heartbeat_file=os.environ.get("IRIS_HEARTBEAT_FILE", "iris-heartbeat.json"),
            heartbeat_state=os.environ.get("IRIS_HEARTBEAT_STATE", "iris-heartbeat.state.json"),
            heartbeat_http_timeout=float(os.environ.get("IRIS_HEARTBEAT_HTTP_TIMEOUT", "15")),
            usage_file=os.environ.get("IRIS_USAGE_FILE", "iris-usage.json"),
            usage_budget_usd=float(os.environ.get("IRIS_USAGE_BUDGET_USD", "0")),
            usage_tighten_at=float(os.environ.get("IRIS_USAGE_TIGHTEN_AT", "80")),
            usage_park_at=float(os.environ.get("IRIS_USAGE_PARK_AT", "95")),
            usage_ping_at=[float(v) for v in _split(os.environ.get("IRIS_USAGE_PING_AT")) or ["50", "80", "95"]],
            tighten_factor=float(os.environ.get("IRIS_TIGHTEN_FACTOR", "3")),
            memory_file=os.environ.get("IRIS_MEMORY_FILE", "iris-memory.json"),
            memory_digest_bytes=int(os.environ.get("IRIS_MEMORY_DIGEST_BYTES", "2400")),
            jobs_digest_bytes=int(os.environ.get("IRIS_JOBS_DIGEST_BYTES", "600")),
            jobs_digest_recent_secs=int(os.environ.get("IRIS_JOBS_DIGEST_RECENT_SECS", "3600")),
            session_store_path=os.environ.get("IRIS_SESSION_STORE", "iris-sessions.json"),
            metrics_file=os.environ.get("IRIS_METRICS_FILE", ""),
            trace_file=os.environ.get("IRIS_TRACE_FILE", ""),
            trace_capture_content=_flag(os.environ.get("IRIS_TRACE_CAPTURE_CONTENT"), False),
            approvals_enabled=_flag(os.environ.get("IRIS_APPROVALS"), False),
            approval_timeout=float(os.environ.get("IRIS_APPROVAL_TIMEOUT", "300")),
            approvals_file=os.environ.get("IRIS_APPROVALS_FILE", "iris-approvals.json"),
            turn_timeout=float(os.environ.get("IRIS_TURN_TIMEOUT", "300")),
            max_retries=int(os.environ.get("IRIS_MAX_RETRIES", "2")),
            retry_base_delay=float(os.environ.get("IRIS_RETRY_BASE_DELAY", "2")),
            timeout_max_retries=int(os.environ.get("IRIS_TIMEOUT_RETRIES", "0")),
            live_interrupt=_flag(os.environ.get("IRIS_LIVE_INTERRUPT"), False),
            stream_idle_timeout=float(os.environ.get("IRIS_STREAM_IDLE_TIMEOUT", "300")),
            stream_total_timeout=float(os.environ.get("IRIS_STREAM_TOTAL_TIMEOUT", "1800")),
            ack_delay=float(os.environ.get("IRIS_ACK_DELAY", "4")),
            compact_at_tokens=int(os.environ.get("IRIS_COMPACT_AT_TOKENS", "150000")),
            compact_every=int(os.environ.get("IRIS_COMPACT_EVERY", "60")),
            compact_seed_turns=int(os.environ.get("IRIS_COMPACT_SEED_TURNS", "16")),
            notify_channel=os.environ.get("IRIS_NOTIFY_CHANNEL", ""),
            watch_min_seconds=float(os.environ.get("IRIS_WATCH_MIN_SECONDS", "30")),
            notify_persona=os.environ.get("IRIS_NOTIFY_PERSONA") or None,
        )
