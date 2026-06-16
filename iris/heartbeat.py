"""The quiet heartbeat: a level-triggered health checklist that reports by exception.

Where a wake fires on an *event* (a log line appears, a URL's content changes),
the heartbeat asks a steady question: are the things that should be true right now
actually true? The owner declares a checklist in ``IRIS_HEARTBEAT_FILE`` (paths and
URLs are owner-authored; the model has no tool to touch the file), the existing
``reminders-tick`` evaluates every check with cheap stat / disk / HTTP calls, and
the result is silent by default. Only when the set of *failing* checks changes does
it send ONE consolidated ping for the whole checklist — so a healthy system is
quiet, a new problem is surfaced once, a steady problem is not repeated, and a
recovery is announced. **It never calls the model**: a fail is a pre-written ping
plus a fold-back inbox note. See docs/superpowers/specs/2026-06-14-heartbeat-design.md.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from .config import Config
from .inbox import Inbox
from .statefile import JsonDictStore

log = logging.getLogger("iris.heartbeat")

KINDS = ("disk_free", "file_fresh", "url_ok")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def validate_checks(checks) -> list[str]:
    """Pure validation: one human-readable line per problem, [] when clean."""
    if not isinstance(checks, list):
        return ["the heartbeat file must hold a JSON list of check objects"]
    problems: list[str] = []
    seen: set = set()
    for index, check in enumerate(checks):
        label = f"check {index}"
        if not isinstance(check, dict):
            problems.append(f"{label}: not an object")
            continue
        name = check.get("name")
        if isinstance(name, str) and name:
            label = f"check {name!r}"
        if not isinstance(name, str) or not _NAME.match(name or ""):
            problems.append(f"{label}: bad name {name!r} (lowercase letters, digits, - or _, max 32)")
        elif name in seen:
            problems.append(f"{label}: duplicate name")
        else:
            seen.add(name)
        kind = check.get("kind")
        if kind not in KINDS:
            problems.append(f"{label}: unknown kind {kind!r} (use one of {', '.join(KINDS)})")
            continue
        if kind == "url_ok":
            url = check.get("url")
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                problems.append(f"{label}: url must be an http(s) URL, got {url!r}")
        else:
            path = check.get("path")
            if not isinstance(path, str) or not os.path.isabs(path):
                problems.append(f"{label}: path must be absolute, got {path!r}")
        if kind == "disk_free":
            pct = check.get("min_percent")
            if not isinstance(pct, (int, float)) or not 0 < pct < 100:
                problems.append(f"{label}: min_percent must be between 0 and 100, got {pct!r}")
        if kind == "file_fresh":
            age = check.get("max_age_secs")
            if not isinstance(age, (int, float)) or age <= 0:
                problems.append(f"{label}: max_age_secs must be a positive number, got {age!r}")
    return problems


def load_checks(path: Path) -> tuple[list, Optional[str]]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"could not read the heartbeat file ({exc})"
    if not isinstance(data, list):
        return [], "could not use the heartbeat file (it must hold a JSON list)"
    return data, None


def http_status(url: str, timeout: float) -> int:
    """GET a URL and return its HTTP status. The default fetcher; tests inject a fake.

    Owner-authored URLs only (no model tool writes the heartbeat file), so there is
    no SSRF guard; the status code is the only thing read, never the body.
    """
    req = urllib.request.Request(
        url, method="GET",
        headers={"User-Agent": "iris-heartbeat (https://github.com/luoojason/iris, 0.1)"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.getcode()


def _evaluate(check: dict, *, now: float, disk_usage=None, fetch=None,
              http_timeout: float = 15.0) -> tuple[bool, str]:
    """Evaluate one check. Returns (ok, detail). Cheap and model-free; an error
    fetching/stat-ing is a failure (with detail), never a raise."""
    kind = check["kind"]

    if kind == "disk_free":
        usage = (disk_usage or shutil.disk_usage)
        try:
            u = usage(check["path"])
            pct = (u.free / u.total * 100.0) if u.total else 0.0
        except OSError as exc:
            return False, f"can't stat {check['path']}: {exc}"
        floor = float(check.get("min_percent", 10))
        if pct < floor:
            return False, f"{pct:.0f}% free < {floor:.0f}% on {check['path']}"
        return True, ""

    if kind == "file_fresh":
        path = Path(check["path"])
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False, f"missing: {check['path']}"
        age = now - mtime
        limit = float(check.get("max_age_secs", 86400))
        if age > limit:
            return False, f"stale: {check['path']} last changed {age / 3600:.1f}h ago"
        return True, ""

    # url_ok
    fetcher = fetch or http_status
    expect = int(check.get("expect_status", 200))
    try:
        status = fetcher(check["url"], http_timeout)
    except Exception as exc:
        return False, f"unreachable: {check['url']} ({exc})"
    if status != expect:
        return False, f"{check['url']} returned {status}, expected {expect}"
    return True, ""


class _StateStore:
    """The tick-owned failing-set state, locked across read-evaluate-write."""

    def __init__(self, path: str | os.PathLike[str]):
        self._store = JsonDictStore(path, "heartbeat state", sort_keys=True)
        self.path = self._store.path

    @contextmanager
    def locked(self):
        with self._store.locked():
            yield self._store.load()

    def save(self, state: dict) -> None:
        self._store.save(state)


def evaluate_all(config: Config, now: float, fetch=None) -> tuple[dict, list[str], int]:
    """Run every valid check once. Returns (failures{name: detail}, skipped, total)."""
    rules_path = Path(config.heartbeat_file)
    checks, problem = load_checks(rules_path)
    if problem:
        return {}, [problem], 0
    failures: dict = {}
    skipped: list[str] = []
    seen: set = set()
    total = 0
    for check in checks:
        problems = validate_checks([check])
        name = check.get("name") if isinstance(check, dict) else None
        if problems:
            skipped.append(f"{name or 'check'} ({problems[0]})")
            continue
        if name in seen:
            skipped.append(f"{name} (duplicate name)")
            continue
        seen.add(name)
        total += 1
        try:
            ok, detail = _evaluate(check, now=now, fetch=fetch,
                                   http_timeout=config.heartbeat_http_timeout)
        except Exception as exc:  # a poisoned check must not abort the others
            log.warning("heartbeat check %s crashed", name, exc_info=True)
            ok, detail = False, f"evaluation crashed: {exc}"
        if not ok:
            failures[name] = detail
    return failures, skipped, total


def tick_heartbeat(config: Config, now: Optional[float] = None, send=None,
                   inbox: Optional[Inbox] = None, fetch=None) -> str:
    """Evaluate the checklist and ping ONCE only when the failing set changes.

    Called from ``iris reminders-tick``. Silent when everything is healthy or a
    known failure is unchanged; one consolidated ping when a check newly fails or
    recovers. Never calls the model; never raises into reminder delivery.
    """
    now = time.time() if now is None else now
    rules_path = Path(config.heartbeat_file)
    if not rules_path.exists():
        return "heartbeat: no checks file"
    if send is None:
        from .reminders import send_discord_message as send
    inbox = inbox or Inbox(config.inbox_file)

    store = _StateStore(config.heartbeat_state)
    pinged = False
    failures: dict = {}
    skipped: list[str] = []
    total = 0
    # Evaluate INSIDE the lock (like wakes), so two overlapping ticks can't both
    # diff against the same prior state and double-ping; save in finally so a
    # crash can't replay an unsaved ping on the next tick.
    with store.locked() as state:
        try:
            failures, skipped, total = evaluate_all(config, now, fetch)
            prev = set(state.get("failing", []))
            current = set(failures)
            state["failing"] = sorted(current)
            if current != prev:
                message = _digest(failures, recovered=sorted(prev - current))
                channel = config.home_channel or config.notify_channel
                # Always fold into the inbox so a failure is never lost, even when
                # there is no channel to ping; push to the channel when there is one.
                inbox.append(message, conversation_id=(f"discord:{channel}" if channel else None))
                if channel and config.discord_token and not send(channel, message, config.discord_token):
                    log.warning("heartbeat could not ping %s", channel)
                pinged = True
        finally:
            store.save(state)

    line = f"heartbeat: {total} checks, {len(failures)} failing"
    if pinged:
        line += " (pinged)"
    if skipped:
        line += "; skipped " + ", ".join(skipped)
    return line


def _digest(failures: dict, recovered: list[str]) -> str:
    if failures:
        lines = [f"heartbeat: {len(failures)} check(s) failing:"]
        lines += [f"- {name}: {detail}" for name, detail in sorted(failures.items())]
        if recovered:
            lines.append("recovered: " + ", ".join(recovered))
        return "\n".join(lines)
    return "heartbeat: all clear" + (
        " (recovered: " + ", ".join(recovered) + ")" if recovered else "")


def doctor_lines(config: Config) -> list[str]:
    """The heartbeat section for `iris doctor`: [] when there is no checks file."""
    rules_path = Path(config.heartbeat_file)
    if not rules_path.exists():
        return []
    checks, problem = load_checks(rules_path)
    if problem:
        return [f"heartbeat: {problem}"]
    problems = validate_checks(checks)
    if not problems:
        return [f"heartbeat: {len(checks)} checks ok"]
    return [f"heartbeat: {len(problems)} problem(s) in {len(checks)} checks:"] + [
        f"  {problem}" for problem in problems
    ]
